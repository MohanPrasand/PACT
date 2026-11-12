"""Shared budget engine.

Invariant, per (scope, period):
    available + reserved + (settled - refunded) == limit

Reservations are taken with a conditional UPDATE (``WHERE limit - reserved - settled + refunded >= amount``)
so two concurrent requests can never both claim the same headroom. A request reserves against every
scope it draws from (org, user, agent) inside a SAVEPOINT: if any scope lacks headroom the whole
reservation rolls back and the caller's outer transaction stays usable (e.g. to record DENIED).
Every movement is also written to an append-only ledger.

Limits are *not* stored: they are passed in at reservation time from the current policy, so a policy
edit applies to the next reservation immediately. Each reservation remembers its own period, so a
request reserved before midnight settles/releases/refunds against the day it was reserved on.

Status changes on a ``Reservation`` are compare-and-set (``WHERE status = 'ACTIVE'``), so releasing or
settling twice -- even from two transactions racing on a database without writer serialisation --
can never move money twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pact_core.db import Clock
from pact_core.models import BudgetCounter, BudgetLedgerEntry, Reservation

LEDGER_KINDS = ("RESERVE", "RELEASE", "SETTLE", "REFUND")


class BudgetExhausted(Exception):
    """Raised by :meth:`BudgetEngine.reserve` when ``scope_key`` lacks headroom. Nothing was reserved."""

    def __init__(self, scope_key: str):
        super().__init__(scope_key)
        self.scope_key = scope_key


def period_for(ts: datetime) -> str:
    """The budget period (UTC calendar day, ``YYYY-MM-DD``) of ``ts``. Naive timestamps are taken as UTC."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC)
    return ts.date().isoformat()


def _require_amount(amount_minor: object, what: str = "amount") -> int:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError(f"{what} must be an int (minor units), got {type(amount_minor).__name__}")
    if amount_minor <= 0:
        raise ValueError(f"{what} must be positive, got {amount_minor}")
    return amount_minor


@dataclass
class ScopeUsage:
    scope_key: str
    period: str
    limit_minor: int
    reserved_minor: int
    settled_minor: int
    refunded_minor: int

    @property
    def net_settled_minor(self) -> int:
        return self.settled_minor - self.refunded_minor

    @property
    def available_minor(self) -> int:
        """May be negative if the limit was lowered below what is already used."""
        return self.limit_minor - self.reserved_minor - self.net_settled_minor

    def as_dict(self) -> dict:
        return {
            "scope": self.scope_key,
            "period": self.period,
            "limit_minor": self.limit_minor,
            "reserved_minor": self.reserved_minor,
            "settled_minor": self.net_settled_minor,
            "refunded_minor": self.refunded_minor,
            "available_minor": self.available_minor,
        }


class BudgetEngine:
    def __init__(self, clock: Clock):
        self.clock = clock

    # ---------------------------------------------------------------- reads
    def usage(self, s: Session, scope_key: str, limit_minor: int, period: str | None = None) -> ScopeUsage:
        period = period or period_for(self.clock())
        C = BudgetCounter
        # Column select (not entities) so we never read stale identity-map state after Core UPDATEs.
        row = s.execute(
            select(C.reserved_minor, C.settled_minor, C.refunded_minor).where(
                C.scope_key == scope_key, C.period == period)
        ).first()
        if row is None:
            return ScopeUsage(scope_key, period, limit_minor, 0, 0, 0)
        return ScopeUsage(scope_key, period, limit_minor, row.reserved_minor, row.settled_minor, row.refunded_minor)

    def used(self, s: Session, scope_key: str, period: str | None = None) -> int:
        """Reserved + net settled for ``scope_key`` in ``period`` (default: today)."""
        u = self.usage(s, scope_key, 0, period)
        return u.reserved_minor + u.net_settled_minor

    def active_reservations(self, s: Session, request_id: str) -> list[Reservation]:
        return self._reservations(s, request_id, "ACTIVE")

    def _reservations(self, s: Session, request_id: str, status: str) -> list[Reservation]:
        return list(
            s.execute(
                select(Reservation)
                .where(Reservation.request_id == request_id, Reservation.status == status)
                .order_by(Reservation.id)
                .execution_options(populate_existing=True)
            ).scalars()
        )

    # ---------------------------------------------------------------- writes
    def _ensure_counter(self, s: Session, scope_key: str, period: str) -> None:
        exists = s.execute(
            select(BudgetCounter.id).where(BudgetCounter.scope_key == scope_key, BudgetCounter.period == period)
        ).first()
        if exists:
            return
        # On SQLite, BEGIN IMMEDIATE serialises writers so the check above cannot race. On PostgreSQL a
        # concurrent creator makes our INSERT fail on uq_budget_scope_period; the savepoint confines
        # that failure and the row now exists for the conditional UPDATE that follows.
        try:
            with s.begin_nested():
                s.add(BudgetCounter(scope_key=scope_key, period=period,
                                    reserved_minor=0, settled_minor=0, refunded_minor=0))
        except IntegrityError:
            pass

    def _ledger(self, s: Session, scope_key: str, period: str, request_id: str, kind: str, amount: int) -> None:
        s.add(BudgetLedgerEntry(scope_key=scope_key, period=period, request_id=request_id, kind=kind,
                                amount_minor=amount, created_at=self.clock()))

    def reserve(self, s: Session, request_id: str, amount_minor: int, scopes: list[tuple[str, int]]) -> None:
        """Atomically reserve ``amount_minor`` against every scope, or nothing. Raises BudgetExhausted.

        ``scopes`` is ``[(scope_key, limit_minor), ...]`` from the *current* policy
        (``pact_core.policy.engine.budget_scopes``). On ``BudgetExhausted`` the savepoint is rolled back:
        no counter, reservation or ledger row is changed and ``s`` remains usable.
        """
        _require_amount(amount_minor)
        if not scopes:
            raise ValueError("reserve requires at least one budget scope")
        keys = [k for k, _ in scopes]
        if len(set(keys)) != len(keys):
            raise ValueError(f"duplicate budget scopes: {keys}")
        for k, limit in scopes:
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise TypeError(f"limit for {k} must be an int, got {type(limit).__name__}")
        held = s.execute(
            select(func.count()).select_from(Reservation).where(
                Reservation.request_id == request_id, Reservation.status.in_(("ACTIVE", "SETTLED")))
        ).scalar_one()
        if held:
            raise ValueError(f"request {request_id} already holds a reservation")

        period = period_for(self.clock())
        for scope_key in keys:
            self._ensure_counter(s, scope_key, period)
        C = BudgetCounter
        with s.begin_nested():  # SAVEPOINT: partial reservations roll back together
            for scope_key, limit in scopes:
                res = s.execute(
                    update(C)
                    .where(
                        C.scope_key == scope_key,
                        C.period == period,
                        (limit - C.reserved_minor - C.settled_minor + C.refunded_minor) >= amount_minor,
                    )
                    .values(reserved_minor=C.reserved_minor + amount_minor)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount != 1:
                    raise BudgetExhausted(scope_key)
                s.add(Reservation(request_id=request_id, scope_key=scope_key, period=period,
                                  amount_minor=amount_minor, status="ACTIVE", refunded_minor=0,
                                  created_at=self.clock()))
                self._ledger(s, scope_key, period, request_id, "RESERVE", amount_minor)
            s.flush()

    def _claim(self, s: Session, r: Reservation, new_status: str, new_amount: int | None = None) -> bool:
        """Compare-and-set ACTIVE -> ``new_status``. False if someone else already moved it."""
        values: dict = {"status": new_status}
        if new_amount is not None:
            values["amount_minor"] = new_amount
        res = s.execute(
            update(Reservation)
            .where(Reservation.id == r.id, Reservation.status == "ACTIVE")
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:
            return False
        s.refresh(r)
        return True

    def release(self, s: Session, request_id: str) -> int:
        """Release every active reservation of a request. Returns the number released (0 if none: idempotent)."""
        n = 0
        for r in self.active_reservations(s, request_id):
            amount = r.amount_minor
            if not self._claim(s, r, "RELEASED"):
                continue
            self._move(s, r, reserved_delta=-amount)
            self._ledger(s, r.scope_key, r.period, request_id, "RELEASE", amount)
            n += 1
        s.flush()
        return n

    def settle(self, s: Session, request_id: str, amount_minor: int | None = None) -> int:
        """Convert active reservations into settled spend (``amount_minor`` defaults to the reserved amount).

        A partial settlement releases the remainder with a RELEASE ledger entry. Settling more than was
        reserved is rejected before anything moves. Returns the number settled (0 if none: idempotent).
        """
        if amount_minor is not None:
            _require_amount(amount_minor)
        active = self.active_reservations(s, request_id)
        if amount_minor is not None:
            for r in active:
                if amount_minor > r.amount_minor:
                    raise ValueError(
                        f"settle {amount_minor} exceeds reserved {r.amount_minor} for {r.scope_key}")
        n = 0
        for r in active:
            reserved = r.amount_minor
            settle_amt = reserved if amount_minor is None else amount_minor
            remainder = reserved - settle_amt
            if not self._claim(s, r, "SETTLED", new_amount=settle_amt):
                continue
            self._move(s, r, reserved_delta=-reserved, settled_delta=settle_amt)
            self._ledger(s, r.scope_key, r.period, request_id, "SETTLE", settle_amt)
            if remainder:
                self._ledger(s, r.scope_key, r.period, request_id, "RELEASE", remainder)
            n += 1
        s.flush()
        return n

    def refund(self, s: Session, request_id: str, amount_minor: int) -> int:
        """Record a refund against settled spend as an explicit REFUND ledger event (history is never deleted).

        The refund is credited to the period the spend was settled in. Cumulative refunds can never exceed
        the settled amount; an over-refund raises ``ValueError`` and changes nothing. Returns the number of
        scopes credited.
        """
        _require_amount(amount_minor)
        settled = self._reservations(s, request_id, "SETTLED")
        if not settled:
            raise ValueError(f"No settled spend to refund for request {request_id}")
        for r in settled:
            refundable = r.amount_minor - r.refunded_minor
            if amount_minor > refundable:
                raise ValueError(f"Refund {amount_minor} exceeds refundable {refundable} for {r.scope_key}")
        R = Reservation
        with s.begin_nested():  # all scopes or none
            for r in settled:
                res = s.execute(
                    update(R)
                    .where(R.id == r.id, R.status == "SETTLED",
                           (R.amount_minor - R.refunded_minor) >= amount_minor)
                    .values(refunded_minor=R.refunded_minor + amount_minor)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount != 1:  # lost a race with a concurrent refund
                    raise ValueError(f"Refund {amount_minor} exceeds refundable amount for {r.scope_key}")
                self._move(s, r, refunded_delta=amount_minor)
                self._ledger(s, r.scope_key, r.period, request_id, "REFUND", amount_minor)
            s.flush()
        for r in settled:
            s.refresh(r)
        return len(settled)

    def _move(self, s: Session, r: Reservation, *, reserved_delta=0, settled_delta=0, refunded_delta=0) -> None:
        C = BudgetCounter
        res = s.execute(
            update(C)
            .where(C.scope_key == r.scope_key, C.period == r.period)
            .values(
                reserved_minor=C.reserved_minor + reserved_delta,
                settled_minor=C.settled_minor + settled_delta,
                refunded_minor=C.refunded_minor + refunded_delta,
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:
            raise RuntimeError(f"budget counter missing for {r.scope_key}/{r.period}")

    # ---------------------------------------------------------------- invariants
    def check_invariants(self, s: Session) -> list[str]:
        """Verify counters == ledger == reservations for every (scope, period). Returns problems ([] if sound).

        Per (scope, period):
          * reserved == sum(ACTIVE reservations) == RESERVE - RELEASE - SETTLE (ledger)
          * settled  == sum(SETTLED reservations) == SETTLE (ledger)
          * refunded == sum(reservation refunds)  == REFUND (ledger)
          * 0 <= refunded <= settled, reserved >= 0
        Together these make ``available + reserved + (settled - refunded) == limit`` hold for any limit.
        """
        problems: list[str] = []
        C, L, R = BudgetCounter, BudgetLedgerEntry, Reservation
        counters = {
            (row.scope_key, row.period): row
            for row in s.execute(select(C.scope_key, C.period, C.reserved_minor, C.settled_minor, C.refunded_minor))
        }
        ledger: dict[tuple[str, str], dict[str, int]] = {}
        for row in s.execute(
            select(L.scope_key, L.period, L.kind, func.sum(L.amount_minor), func.min(L.amount_minor))
            .group_by(L.scope_key, L.period, L.kind)
        ):
            key = (row[0], row[1])
            if row[2] not in LEDGER_KINDS:
                problems.append(f"{key[0]}/{key[1]}: unknown ledger kind {row[2]!r}")
            if row[4] is not None and row[4] < 0:
                problems.append(f"{key[0]}/{key[1]}: negative {row[2]} ledger entry")
            ledger.setdefault(key, {})[row[2]] = int(row[3] or 0)
        resv: dict[tuple[str, str], dict[str, int]] = {}
        for row in s.execute(
            select(R.scope_key, R.period, R.status, func.sum(R.amount_minor), func.sum(R.refunded_minor))
            .group_by(R.scope_key, R.period, R.status)
        ):
            key = (row[0], row[1])
            d = resv.setdefault(key, {})
            d[row[2]] = int(row[3] or 0)
            d["refunded"] = d.get("refunded", 0) + int(row[4] or 0)
            if row[2] not in ("ACTIVE", "SETTLED", "RELEASED"):
                problems.append(f"{key[0]}/{key[1]}: unknown reservation status {row[2]!r}")
        for row in s.execute(
            select(R.id, R.scope_key, R.period, R.status, R.amount_minor, R.refunded_minor).where(
                (R.refunded_minor < 0) | (R.refunded_minor > R.amount_minor) | (R.amount_minor < 0)
                | ((R.status != "SETTLED") & (R.refunded_minor != 0)))
        ):
            problems.append(f"{row.scope_key}/{row.period}: reservation {row.id} ({row.status}) has "
                            f"amount {row.amount_minor}, refunded {row.refunded_minor}")

        for key in sorted(set(counters) | set(ledger) | set(resv)):
            name = f"{key[0]}/{key[1]}"
            c = counters.get(key)
            lg = ledger.get(key, {})
            rv = resv.get(key, {})
            if c is None:
                problems.append(f"{name}: ledger/reservations exist without a budget counter")
                continue
            reserve, release, settle, refund = (lg.get(k, 0) for k in LEDGER_KINDS)
            active, settled_resv, refunded_resv = rv.get("ACTIVE", 0), rv.get("SETTLED", 0), rv.get("refunded", 0)
            if c.reserved_minor != active:
                problems.append(f"{name}: reserved {c.reserved_minor} != active reservations {active}")
            if c.reserved_minor != reserve - release - settle:
                problems.append(f"{name}: reserved {c.reserved_minor} != ledger reserve-release-settle "
                                f"{reserve - release - settle}")
            if c.settled_minor != settle:
                problems.append(f"{name}: settled {c.settled_minor} != ledger {settle}")
            if c.settled_minor != settled_resv:
                problems.append(f"{name}: settled {c.settled_minor} != settled reservations {settled_resv}")
            if c.refunded_minor != refund:
                problems.append(f"{name}: refunded {c.refunded_minor} != ledger {refund}")
            if c.refunded_minor != refunded_resv:
                problems.append(f"{name}: refunded {c.refunded_minor} != reservation refunds {refunded_resv}")
            if c.reserved_minor < 0 or c.refunded_minor < 0 or c.settled_minor - c.refunded_minor < 0:
                problems.append(f"{name}: negative balance")
        return problems
