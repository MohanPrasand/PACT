"""Shared budget engine.

Invariant, per (scope, period):
    available + reserved + (settled - refunded) == limit

Reservations are taken with a conditional UPDATE (``WHERE limit - reserved - settled + refunded >= amount``)
so two concurrent requests can never both claim the same headroom. A request reserves against every
scope it draws from (org, user, agent) inside a SAVEPOINT: if any scope lacks headroom the whole
reservation rolls back. Every movement is also written to an append-only ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pact_core.db import Clock
from pact_core.models import BudgetCounter, BudgetLedgerEntry, Reservation


class BudgetExhausted(Exception):
    def __init__(self, scope_key: str):
        super().__init__(scope_key)
        self.scope_key = scope_key


def period_for(ts: datetime) -> str:
    return ts.date().isoformat()


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
        c = s.execute(
            select(BudgetCounter).where(BudgetCounter.scope_key == scope_key, BudgetCounter.period == period)
        ).scalar_one_or_none()
        if c is None:
            return ScopeUsage(scope_key, period, limit_minor, 0, 0, 0)
        return ScopeUsage(scope_key, period, limit_minor, c.reserved_minor, c.settled_minor, c.refunded_minor)

    def used(self, s: Session, scope_key: str, period: str | None = None) -> int:
        u = self.usage(s, scope_key, 0, period)
        return u.reserved_minor + u.net_settled_minor

    def active_reservations(self, s: Session, request_id: str) -> list[Reservation]:
        return list(
            s.execute(
                select(Reservation).where(Reservation.request_id == request_id, Reservation.status == "ACTIVE")
            ).scalars()
        )

    # ---------------------------------------------------------------- writes
    def _ensure_counter(self, s: Session, scope_key: str, period: str) -> None:
        exists = s.execute(
            select(BudgetCounter.id).where(BudgetCounter.scope_key == scope_key, BudgetCounter.period == period)
        ).first()
        if exists:
            return
        try:
            with s.begin_nested():
                s.add(BudgetCounter(scope_key=scope_key, period=period))
        except IntegrityError:  # created concurrently (PostgreSQL); fine
            pass

    def _ledger(self, s: Session, scope_key: str, period: str, request_id: str, kind: str, amount: int) -> None:
        s.add(BudgetLedgerEntry(scope_key=scope_key, period=period, request_id=request_id, kind=kind,
                                amount_minor=amount, created_at=self.clock()))

    def reserve(self, s: Session, request_id: str, amount_minor: int, scopes: list[tuple[str, int]]) -> None:
        """Atomically reserve ``amount_minor`` against every scope, or nothing. Raises BudgetExhausted."""
        if amount_minor <= 0:
            raise ValueError("amount must be positive")
        period = period_for(self.clock())
        for scope_key, _ in scopes:
            self._ensure_counter(s, scope_key, period)
        with s.begin_nested():  # SAVEPOINT: partial reservations roll back together
            for scope_key, limit in scopes:
                C = BudgetCounter
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
                                  amount_minor=amount_minor, created_at=self.clock()))
                self._ledger(s, scope_key, period, request_id, "RESERVE", amount_minor)

    def release(self, s: Session, request_id: str) -> int:
        """Release every active reservation of a request. Returns the number released."""
        n = 0
        for r in self.active_reservations(s, request_id):
            self._move(s, r, reserved_delta=-r.amount_minor)
            r.status = "RELEASED"
            self._ledger(s, r.scope_key, r.period, request_id, "RELEASE", r.amount_minor)
            n += 1
        return n

    def settle(self, s: Session, request_id: str, amount_minor: int | None = None) -> int:
        """Convert active reservations into settled spend. Any over-reservation is released."""
        n = 0
        for r in self.active_reservations(s, request_id):
            settle_amt = r.amount_minor if amount_minor is None else min(amount_minor, r.amount_minor)
            remainder = r.amount_minor - settle_amt
            self._move(s, r, reserved_delta=-r.amount_minor, settled_delta=settle_amt)
            r.status = "SETTLED"
            r.amount_minor = settle_amt
            self._ledger(s, r.scope_key, r.period, request_id, "SETTLE", settle_amt)
            if remainder:
                self._ledger(s, r.scope_key, r.period, request_id, "RELEASE", remainder)
            n += 1
        return n

    def refund(self, s: Session, request_id: str, amount_minor: int) -> int:
        """Record a refund against settled spend as an explicit ledger event (history is never deleted)."""
        settled = list(
            s.execute(select(Reservation).where(Reservation.request_id == request_id, Reservation.status == "SETTLED")).scalars()
        )
        if not settled:
            raise ValueError("No settled spend to refund for this request")
        for r in settled:
            refundable = r.amount_minor - r.refunded_minor
            if amount_minor > refundable:
                raise ValueError(f"Refund {amount_minor} exceeds refundable {refundable} for {r.scope_key}")
        for r in settled:
            self._move(s, r, refunded_delta=amount_minor)
            r.refunded_minor += amount_minor
            self._ledger(s, r.scope_key, r.period, request_id, "REFUND", amount_minor)
        return len(settled)

    def _move(self, s: Session, r: Reservation, *, reserved_delta=0, settled_delta=0, refunded_delta=0) -> None:
        C = BudgetCounter
        s.execute(
            update(C)
            .where(C.scope_key == r.scope_key, C.period == r.period)
            .values(
                reserved_minor=C.reserved_minor + reserved_delta,
                settled_minor=C.settled_minor + settled_delta,
                refunded_minor=C.refunded_minor + refunded_delta,
            )
            .execution_options(synchronize_session=False)
        )

    # ---------------------------------------------------------------- invariants
    def check_invariants(self, s: Session) -> list[str]:
        """Counters must equal the ledger, and reserved must equal the sum of active reservations."""
        problems: list[str] = []
        for c in s.execute(select(BudgetCounter)).scalars():
            def ledger_sum(kind: str) -> int:
                return s.execute(
                    select(func.coalesce(func.sum(BudgetLedgerEntry.amount_minor), 0)).where(
                        BudgetLedgerEntry.scope_key == c.scope_key, BudgetLedgerEntry.period == c.period,
                        BudgetLedgerEntry.kind == kind)
                ).scalar_one()
            reserve, release, settle, refund = (ledger_sum(k) for k in ("RESERVE", "RELEASE", "SETTLE", "REFUND"))
            active = s.execute(
                select(func.coalesce(func.sum(Reservation.amount_minor), 0)).where(
                    Reservation.scope_key == c.scope_key, Reservation.period == c.period, Reservation.status == "ACTIVE")
            ).scalar_one()
            if c.reserved_minor != active:
                problems.append(f"{c.scope_key}/{c.period}: reserved {c.reserved_minor} != active reservations {active}")
            settled_reservations = s.execute(
                select(func.coalesce(func.sum(Reservation.amount_minor), 0)).where(
                    Reservation.scope_key == c.scope_key, Reservation.period == c.period, Reservation.status == "SETTLED")
            ).scalar_one()
            if c.settled_minor != settle or settle != settled_reservations:
                problems.append(f"{c.scope_key}/{c.period}: settled {c.settled_minor} != ledger {settle}")
            if c.refunded_minor != refund:
                problems.append(f"{c.scope_key}/{c.period}: refunded {c.refunded_minor} != ledger {refund}")
            if reserve - release - settle != c.reserved_minor:
                problems.append(f"{c.scope_key}/{c.period}: ledger reserve-release-settle != reserved")
            if c.reserved_minor < 0 or c.settled_minor - c.refunded_minor < 0:
                problems.append(f"{c.scope_key}/{c.period}: negative balance")
        return problems
