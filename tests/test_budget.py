"""#6 Shared budget engine with atomic reservations (spec §2, §7.3, §14.2)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update

from pact_core.budget import BudgetEngine, BudgetExhausted, ScopeUsage, period_for
from pact_core.models import Agent, AuthorizationRequest, BudgetCounter, BudgetLedgerEntry, Reservation, State

DAY1 = "2026-11-12"  # FixedClock starts 2026-11-12 09:00 UTC
DAY2 = "2026-11-13"
ORG = "org:global"
USER = "user:u1"


# ---------------------------------------------------------------- helpers
def _run_threads(n: int, target) -> list[BaseException]:
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def wrapped(i):
        try:
            start.wait()
            target(i)
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return errors


def _add_requests(db, *request_ids: str, agent_id: str = "ag_shopping", amount: int = 100) -> None:
    with db.transaction() as s:
        if s.get(Agent, agent_id) is None:
            s.add(Agent(id=agent_id, name=agent_id, user_id="u1", api_key_hash=f"hash-{agent_id}"))
        for rid in request_ids:
            s.add(AuthorizationRequest(
                id=rid, agent_id=agent_id, user_id="u1", merchant_id="m1", order_id=f"o-{rid}",
                action_type="purchase", amount_minor=amount, currency="INR", request_hash="h" * 64,
                state=State.CREATED,
            ))


def _ledger(s, request_id: str | None = None) -> list[tuple[str, str, int]]:
    q = select(BudgetLedgerEntry.scope_key, BudgetLedgerEntry.kind, BudgetLedgerEntry.amount_minor).order_by(
        BudgetLedgerEntry.id)
    if request_id is not None:
        q = q.where(BudgetLedgerEntry.request_id == request_id)
    return [tuple(r) for r in s.execute(q)]


def _reservations(s, request_id: str) -> list[tuple[str, str, int, int]]:
    return [tuple(r) for r in s.execute(
        select(Reservation.scope_key, Reservation.status, Reservation.amount_minor, Reservation.refunded_minor)
        .where(Reservation.request_id == request_id).order_by(Reservation.id))]


def _scopes(org=10_000, user=10_000, agent=5_000, agent_id="ag_shopping"):
    return [(ORG, org), (USER, user), (f"agent:{agent_id}", agent)]


@pytest.fixture
def engine(clock) -> BudgetEngine:
    return BudgetEngine(clock)


def assert_sound(db, engine):
    with db.transaction() as s:
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- pure helpers
def test_period_for_naive_is_utc_day():
    assert period_for(datetime(2026, 11, 12, 23, 59, 59)) == "2026-11-12"
    assert period_for(datetime(2026, 11, 13, 0, 0, 0)) == "2026-11-13"


def test_period_for_aware_timestamps_use_utc_day():
    est = timezone(timedelta(hours=-5))
    assert period_for(datetime(2026, 11, 12, 22, 0, tzinfo=est)) == "2026-11-13"
    ist = timezone(timedelta(hours=5, minutes=30))
    assert period_for(datetime(2026, 11, 13, 3, 0, tzinfo=ist)) == "2026-11-12"


def test_scope_usage_invariant_and_dict():
    u = ScopeUsage("org:global", DAY1, 1000, reserved_minor=200, settled_minor=500, refunded_minor=100)
    assert u.net_settled_minor == 400
    assert u.available_minor == 400
    assert u.available_minor + u.reserved_minor + u.net_settled_minor == u.limit_minor
    assert u.as_dict() == {
        "scope": "org:global", "period": DAY1, "limit_minor": 1000, "reserved_minor": 200,
        "settled_minor": 400, "refunded_minor": 100, "available_minor": 400,
    }


# ---------------------------------------------------------------- reserve
def test_reserve_moves_every_scope_and_writes_ledger(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1500, _scopes())
    with db.transaction() as s:
        for key, limit in _scopes():
            u = engine.usage(s, key, limit)
            assert (u.period, u.reserved_minor, u.settled_minor, u.refunded_minor) == (DAY1, 1500, 0, 0)
            assert u.available_minor == limit - 1500
            assert engine.used(s, key) == 1500
        assert _reservations(s, "r1") == [(k, "ACTIVE", 1500, 0) for k, _ in _scopes()]
        assert _ledger(s, "r1") == [(k, "RESERVE", 1500) for k, _ in _scopes()]
        assert [r.scope_key for r in engine.active_reservations(s, "r1")] == [k for k, _ in _scopes()]
        assert engine.check_invariants(s) == []


def test_usage_of_untouched_scope_is_zero(db, engine):
    with db.transaction() as s:
        u = engine.usage(s, "agent:nobody", 700)
        assert (u.period, u.reserved_minor, u.settled_minor, u.refunded_minor, u.available_minor) == (
            DAY1, 0, 0, 0, 700)
        assert engine.used(s, "agent:nobody") == 0
        assert engine.active_reservations(s, "nope") == []


def test_reserve_exact_fit_then_one_more_unit_is_denied(db, engine):
    _add_requests(db, "r1", "r2", "r3")
    with db.transaction() as s:
        engine.reserve(s, "r1", 600, [(ORG, 1000)])
        engine.reserve(s, "r2", 400, [(ORG, 1000)])  # exactly fills
        with pytest.raises(BudgetExhausted) as ei:
            engine.reserve(s, "r3", 1, [(ORG, 1000)])
        assert ei.value.scope_key == ORG
        assert engine.usage(s, ORG, 1000).available_minor == 0
    assert_sound(db, engine)


def test_reserve_larger_than_limit_is_denied(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        with pytest.raises(BudgetExhausted):
            engine.reserve(s, "r1", 1001, [(ORG, 1000)])
        assert engine.used(s, ORG) == 0
    assert_sound(db, engine)


@pytest.mark.parametrize("amount, exc", [(0, ValueError), (-5, ValueError), (True, TypeError), (1.5, TypeError),
                                          ("100", TypeError), (None, TypeError)])
def test_reserve_rejects_bad_amounts(db, engine, amount, exc):
    _add_requests(db, "r1")
    with db.transaction() as s:
        with pytest.raises(exc):
            engine.reserve(s, "r1", amount, _scopes())
        assert _ledger(s) == [] and _reservations(s, "r1") == []


@pytest.mark.parametrize("scopes, exc", [
    ([], ValueError),
    ([(ORG, 1000), (ORG, 1000)], ValueError),
    ([(ORG, 10.5)], TypeError),
    ([(ORG, None)], TypeError),
])
def test_reserve_rejects_bad_scopes(db, engine, scopes, exc):
    _add_requests(db, "r1")
    with db.transaction() as s:
        with pytest.raises(exc):
            engine.reserve(s, "r1", 100, scopes)
        assert _ledger(s) == [] and _reservations(s, "r1") == []
    assert_sound(db, engine)


def test_reserve_twice_for_same_request_is_rejected(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 100, _scopes())
        with pytest.raises(ValueError, match="already holds"):
            engine.reserve(s, "r1", 100, _scopes())
        assert engine.used(s, ORG) == 100
        engine.settle(s, "r1")
        with pytest.raises(ValueError, match="already holds"):
            engine.reserve(s, "r1", 100, _scopes())
        assert engine.used(s, ORG) == 100
    assert_sound(db, engine)


def test_reserve_again_after_release_is_allowed(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 100, _scopes())
        engine.release(s, "r1")
        engine.reserve(s, "r1", 250, _scopes())
        assert engine.used(s, ORG) == 250
        assert engine.check_invariants(s) == []


def test_reads_are_fresh_within_the_same_session(db, engine):
    """Counter entities held in the identity map must not make usage()/check_invariants() stale."""
    _add_requests(db, "r1", "r2")
    with db.transaction() as s:
        engine.reserve(s, "r1", 100, [(ORG, 1000)])
    with db.transaction() as s:
        held = s.execute(select(BudgetCounter).where(BudgetCounter.scope_key == ORG)).scalar_one()
        assert held.reserved_minor == 100
        engine.reserve(s, "r2", 200, [(ORG, 1000)])
        assert engine.usage(s, ORG, 1000).reserved_minor == 300
        engine.settle(s, "r2", 150)
        u = engine.usage(s, ORG, 1000)
        assert (u.reserved_minor, u.settled_minor) == (100, 150)
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- concurrency
def test_twenty_threads_race_for_budget_fitting_exactly_five(db, engine):
    n, amount = 20, 1_000
    ids = [f"race-{i}" for i in range(n)]
    _add_requests(db, *ids)
    wins: list[int] = []
    denied: list[str] = []
    lock = threading.Lock()

    def worker(i):
        with db.transaction() as s:
            try:
                engine.reserve(s, ids[i], amount, [(ORG, 5 * amount), (USER, 100_000), ("agent:ag_shopping", 100_000)])
            except BudgetExhausted as exc:
                with lock:
                    denied.append(exc.scope_key)
                return
        with lock:
            wins.append(i)

    assert _run_threads(n, worker) == []
    assert len(wins) == 5
    assert denied == [ORG] * 15
    with db.transaction() as s:
        assert engine.usage(s, ORG, 5 * amount).reserved_minor == 5 * amount
        assert engine.usage(s, ORG, 5 * amount).available_minor == 0
        # every scope moved for winners only; no partial reservations for losers
        assert engine.used(s, USER) == 5 * amount
        assert engine.used(s, "agent:ag_shopping") == 5 * amount
        assert len(_ledger(s)) == 5 * 3
        for i in range(n):
            assert len(_reservations(s, ids[i])) == (3 if i in wins else 0)
        assert engine.check_invariants(s) == []


def test_concurrent_settle_and_release_move_money_once(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 500, _scopes())
    results: list[tuple[str, int]] = []
    lock = threading.Lock()

    def worker(i):
        with db.transaction() as s:
            n = engine.settle(s, "r1") if i % 2 == 0 else engine.release(s, "r1")
        with lock:
            results.append(("settle" if i % 2 == 0 else "release", n))

    assert _run_threads(8, worker) == []
    assert sorted(n for _, n in results) == [0] * 7 + [3]
    with db.transaction() as s:
        u = engine.usage(s, ORG, 10_000)
        assert u.reserved_minor == 0
        assert u.settled_minor in (0, 500)
        assert engine.check_invariants(s) == []


def test_concurrent_refunds_never_exceed_settled(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1000, _scopes())
        engine.settle(s, "r1")
    ok: list[int] = []
    lock = threading.Lock()

    def worker(i):
        with db.transaction() as s:
            try:
                engine.refund(s, "r1", 300)
            except ValueError:
                return
        with lock:
            ok.append(i)

    assert _run_threads(6, worker) == []
    assert len(ok) == 3
    with db.transaction() as s:
        assert engine.usage(s, ORG, 10_000).refunded_minor == 900
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- partial failure / savepoint
@pytest.mark.parametrize("failing", ["agent", "user", "org"])
def test_partial_reservation_failure_reserves_nothing_and_outer_tx_stays_usable(db, engine, failing):
    _add_requests(db, "big", "r1")
    limits = {"org": 10_000, "user": 10_000, "agent": 3_000}
    with db.transaction() as s:  # pre-load the failing scope so it has exactly 500 left
        prior = {"org": 9_500, "user": 9_500, "agent": 2_500}[failing]
        engine.reserve(s, "big", prior, [({"org": ORG, "user": USER, "agent": "agent:ag_shopping"}[failing],
                                          limits[failing])])
    with db.transaction() as s:
        before = {k: engine.used(s, k) for k in (ORG, USER, "agent:ag_shopping")}
        ledger_before = len(_ledger(s))
        with pytest.raises(BudgetExhausted) as ei:
            engine.reserve(s, "r1", 1_000, [(ORG, limits["org"]), (USER, limits["user"]),
                                             ("agent:ag_shopping", limits["agent"])])
        assert ei.value.scope_key == {"org": ORG, "user": USER, "agent": "agent:ag_shopping"}[failing]
        # nothing reserved anywhere, inside the still-open transaction
        assert {k: engine.used(s, k) for k in before} == before
        assert _reservations(s, "r1") == []
        assert len(_ledger(s)) == ledger_before
        # the outer transaction is still usable: record the denial
        s.execute(update(AuthorizationRequest).where(AuthorizationRequest.id == "r1").values(state=State.DENIED))
    with db.transaction() as s:
        assert s.get(AuthorizationRequest, "r1").state == State.DENIED
        assert {k: engine.used(s, k) for k in before} == before
        assert _reservations(s, "r1") == []
        assert engine.check_invariants(s) == []


def test_failed_reserve_as_first_statement_of_transaction(db, engine):
    """The SAVEPOINT nests inside SQLite's BEGIN IMMEDIATE even when reserve() opens the transaction."""
    _add_requests(db, "r1", "r2")
    with db.transaction() as s:
        with pytest.raises(BudgetExhausted):
            engine.reserve(s, "r1", 50, [(ORG, 10)])
        engine.reserve(s, "r2", 10, [(ORG, 10)])  # session still fully usable
    with db.transaction() as s:
        assert engine.used(s, ORG) == 10
        assert _reservations(s, "r1") == []
        assert engine.check_invariants(s) == []


def test_outer_rollback_discards_reservation(db, engine):
    _add_requests(db, "r1")
    with pytest.raises(RuntimeError):
        with db.transaction() as s:
            engine.reserve(s, "r1", 100, _scopes())
            raise RuntimeError("caller failed after reserving")
    with db.transaction() as s:
        assert engine.used(s, ORG) == 0
        assert _ledger(s) == []
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- settle / release
def test_full_settlement_moves_reserved_to_settled(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 2_000, _scopes())
    with db.transaction() as s:
        assert engine.settle(s, "r1") == 3
    with db.transaction() as s:
        for key, limit in _scopes():
            u = engine.usage(s, key, limit)
            assert (u.reserved_minor, u.settled_minor, u.available_minor) == (0, 2_000, limit - 2_000)
        assert _reservations(s, "r1") == [(k, "SETTLED", 2_000, 0) for k, _ in _scopes()]
        assert [k for _, k, _ in _ledger(s, "r1")] == ["RESERVE"] * 3 + ["SETTLE"] * 3
        assert engine.active_reservations(s, "r1") == []
        assert engine.check_invariants(s) == []


def test_partial_settlement_releases_remainder(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1_000, _scopes())
        assert engine.settle(s, "r1", 600) == 3
    with db.transaction() as s:
        for key, limit in _scopes():
            u = engine.usage(s, key, limit)
            assert (u.reserved_minor, u.settled_minor, u.available_minor) == (0, 600, limit - 600)
        assert _reservations(s, "r1") == [(k, "SETTLED", 600, 0) for k, _ in _scopes()]
        settle_entries = [e for e in _ledger(s, "r1") if e[1] != "RESERVE"]
        assert settle_entries == [e for k, _ in _scopes() for e in ((k, "SETTLE", 600), (k, "RELEASE", 400))]
        assert engine.check_invariants(s) == []


def test_settle_exact_reserved_amount_has_no_release_entry(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1_000, [(ORG, 5_000)])
        engine.settle(s, "r1", 1_000)
        assert _ledger(s, "r1") == [(ORG, "RESERVE", 1_000), (ORG, "SETTLE", 1_000)]


@pytest.mark.parametrize("amount, exc", [(1_001, ValueError), (0, ValueError), (-1, ValueError), (2.0, TypeError)])
def test_settle_rejects_bad_amounts_without_moving_anything(db, engine, amount, exc):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1_000, _scopes())
    with db.transaction() as s:
        with pytest.raises(exc):
            engine.settle(s, "r1", amount)
        assert engine.usage(s, ORG, 10_000).reserved_minor == 1_000
        assert len(engine.active_reservations(s, "r1")) == 3
        assert engine.check_invariants(s) == []


def test_failed_payment_releases_reservation(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 3_000, _scopes())
    with db.transaction() as s:  # provider reported failure (state reconciled) -> release
        assert engine.release(s, "r1") == 3
    with db.transaction() as s:
        for key, limit in _scopes():
            u = engine.usage(s, key, limit)
            assert (u.reserved_minor, u.settled_minor, u.available_minor) == (0, 0, limit)
        assert _reservations(s, "r1") == [(k, "RELEASED", 3_000, 0) for k, _ in _scopes()]
        assert [e for e in _ledger(s, "r1") if e[1] == "RELEASE"] == [(k, "RELEASE", 3_000) for k, _ in _scopes()]
        assert engine.check_invariants(s) == []


def test_cancellation_of_pending_approval_releases_reservation(db, engine):
    """A pending approval reserves its maximum amount; rejecting/expiring it frees the headroom for others."""
    _add_requests(db, "pending", "other")
    with db.transaction() as s:
        engine.reserve(s, "pending", 8_000, [(ORG, 10_000)])
        with pytest.raises(BudgetExhausted):
            engine.reserve(s, "other", 3_000, [(ORG, 10_000)])
    with db.transaction() as s:
        assert engine.release(s, "pending") == 1
        engine.reserve(s, "other", 3_000, [(ORG, 10_000)])
        assert engine.used(s, ORG) == 3_000
        assert engine.check_invariants(s) == []


def test_double_release_and_double_settle_are_noops(db, engine):
    _add_requests(db, "a", "b")
    with db.transaction() as s:
        engine.reserve(s, "a", 100, _scopes())
        engine.reserve(s, "b", 200, _scopes())
    with db.transaction() as s:
        assert engine.release(s, "a") == 3
    with db.transaction() as s:
        assert engine.release(s, "a") == 0
        assert engine.settle(s, "b") == 3
    with db.transaction() as s:
        assert engine.settle(s, "b") == 0
        assert engine.settle(s, "b", 50) == 0
        u = engine.usage(s, ORG, 10_000)
        assert (u.reserved_minor, u.settled_minor) == (0, 200)
        assert len(_ledger(s)) == 3 * 4  # 2 RESERVE + RELEASE + SETTLE per scope, nothing extra
        assert engine.check_invariants(s) == []


def test_release_after_settle_and_settle_after_release_are_noops(db, engine):
    _add_requests(db, "settled", "released")
    with db.transaction() as s:
        engine.reserve(s, "settled", 100, _scopes())
        engine.reserve(s, "released", 200, _scopes())
        engine.settle(s, "settled", 70)
        engine.release(s, "released")
        ledger_len = len(_ledger(s))
        assert engine.release(s, "settled") == 0
        assert engine.settle(s, "released") == 0
        assert len(_ledger(s)) == ledger_len
        u = engine.usage(s, ORG, 10_000)
        assert (u.reserved_minor, u.settled_minor) == (0, 70)
        assert _reservations(s, "settled") == [(k, "SETTLED", 70, 0) for k, _ in _scopes()]
        assert _reservations(s, "released") == [(k, "RELEASED", 200, 0) for k, _ in _scopes()]
        assert engine.check_invariants(s) == []


def test_release_and_settle_of_unknown_request_are_noops(db, engine):
    with db.transaction() as s:
        assert engine.release(s, "ghost") == 0
        assert engine.settle(s, "ghost") == 0
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- refunds
def test_full_refund_is_explicit_ledger_event_and_restores_available(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1_000, _scopes())
        engine.settle(s, "r1")
    with db.transaction() as s:
        assert engine.refund(s, "r1", 1_000) == 3
    with db.transaction() as s:
        for key, limit in _scopes():
            u = engine.usage(s, key, limit)
            assert (u.reserved_minor, u.settled_minor, u.refunded_minor) == (0, 1_000, 1_000)
            assert u.net_settled_minor == 0 and u.available_minor == limit
            assert engine.used(s, key) == 0
        # history is kept: SETTLE stays, REFUND is appended
        assert [k for _, k, _ in _ledger(s, "r1")] == ["RESERVE"] * 3 + ["SETTLE"] * 3 + ["REFUND"] * 3
        assert _reservations(s, "r1") == [(k, "SETTLED", 1_000, 1_000) for k, _ in _scopes()]
        assert engine.check_invariants(s) == []


def test_multiple_partial_refunds_then_over_refund_rejected(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1_000, _scopes())
        engine.settle(s, "r1")
    with db.transaction() as s:
        engine.refund(s, "r1", 300)
    with db.transaction() as s:
        engine.refund(s, "r1", 200)
    with db.transaction() as s:
        assert engine.usage(s, ORG, 10_000).available_minor == 10_000 - 500
        with pytest.raises(ValueError, match="exceeds refundable 500"):
            engine.refund(s, "r1", 501)
        # rejected refund changed nothing and the session is still usable
        assert engine.usage(s, ORG, 10_000).refunded_minor == 500
        engine.refund(s, "r1", 500)
    with db.transaction() as s:
        assert engine.usage(s, ORG, 10_000).refunded_minor == 1_000
        assert [a for _, k, a in _ledger(s, "r1") if k == "REFUND"] == [300] * 3 + [200] * 3 + [500] * 3
        with pytest.raises(ValueError):
            engine.refund(s, "r1", 1)
        assert engine.check_invariants(s) == []


def test_refund_is_capped_by_partially_settled_amount(db, engine):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1_000, _scopes())
        engine.settle(s, "r1", 400)
        with pytest.raises(ValueError):
            engine.refund(s, "r1", 401)
        engine.refund(s, "r1", 400)
        assert engine.used(s, ORG) == 0
        assert engine.check_invariants(s) == []


@pytest.mark.parametrize("amount, exc", [(0, ValueError), (-100, ValueError), (True, TypeError), (1.0, TypeError)])
def test_refund_rejects_bad_amounts(db, engine, amount, exc):
    _add_requests(db, "r1")
    with db.transaction() as s:
        engine.reserve(s, "r1", 1_000, _scopes())
        engine.settle(s, "r1")
        with pytest.raises(exc):
            engine.refund(s, "r1", amount)
        u = engine.usage(s, ORG, 10_000)
        assert (u.settled_minor, u.refunded_minor) == (1_000, 0)
        assert engine.check_invariants(s) == []


def test_refund_requires_settled_spend(db, engine):
    _add_requests(db, "active", "released")
    with db.transaction() as s:
        engine.reserve(s, "active", 100, _scopes())
        engine.reserve(s, "released", 100, _scopes())
        engine.release(s, "released")
        for rid in ("active", "released", "ghost"):
            with pytest.raises(ValueError, match="No settled spend"):
                engine.refund(s, rid, 10)
        assert engine.usage(s, ORG, 10_000).refunded_minor == 0
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- spec §2 cross-agent contention
def test_cross_agent_contention_under_one_org_budget(db, engine):
    """Org ₹10,000. Shopping ₹4,000 + Research ₹3,000 fit; Procurement ₹5,000 is within its own cap but the
    shared org budget has only ₹3,000 left, so it is denied on org:global. Releasing frees headroom."""
    org = 1_000_000  # ₹10,000.00 in paise
    caps = {"ag_shopping": 500_000, "ag_research": 300_000, "ag_procurement": 500_000}
    for agent, rid in (("ag_shopping", "shop"), ("ag_research", "res"), ("ag_procurement", "proc")):
        _add_requests(db, rid, agent_id=agent)

    def scopes(agent):
        return [(ORG, org), (USER, 2_000_000), (f"agent:{agent}", caps[agent])]

    with db.transaction() as s:
        engine.reserve(s, "shop", 400_000, scopes("ag_shopping"))
    with db.transaction() as s:
        engine.reserve(s, "res", 300_000, scopes("ag_research"))
    with db.transaction() as s:
        with pytest.raises(BudgetExhausted) as ei:
            engine.reserve(s, "proc", 500_000, scopes("ag_procurement"))
        assert ei.value.scope_key == ORG
        assert engine.usage(s, "agent:ag_procurement", caps["ag_procurement"]).available_minor == 500_000
        assert engine.usage(s, ORG, org).available_minor == 300_000
        assert _reservations(s, "proc") == []
    assert_sound(db, engine)

    with db.transaction() as s:  # research's purchase fails -> its reservation is released
        assert engine.release(s, "res") == 3
    with db.transaction() as s:
        engine.reserve(s, "proc", 500_000, scopes("ag_procurement"))
    with db.transaction() as s:
        engine.settle(s, "shop")
        engine.settle(s, "proc")
    with db.transaction() as s:
        u = engine.usage(s, ORG, org)
        assert (u.reserved_minor, u.settled_minor, u.available_minor) == (0, 900_000, 100_000)
        assert engine.used(s, "agent:ag_shopping") == 400_000
        assert engine.used(s, "agent:ag_research") == 0
        assert engine.used(s, "agent:ag_procurement") == 500_000
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- periods & policy changes
def test_period_rollover_settles_against_reservation_day(db, engine, clock):
    _add_requests(db, "r1", "r2")
    with db.transaction() as s:
        engine.reserve(s, "r1", 4_000, [(ORG, 5_000)])
    clock.advance(days=1)
    with db.transaction() as s:
        assert engine.usage(s, ORG, 5_000).period == DAY2
        assert engine.used(s, ORG) == 0  # day-2 starts at zero
        assert engine.settle(s, "r1", 3_000) == 1
    with db.transaction() as s:
        d1 = engine.usage(s, ORG, 5_000, period=DAY1)
        assert (d1.reserved_minor, d1.settled_minor) == (0, 3_000)
        assert engine.used(s, ORG) == 0
        engine.reserve(s, "r2", 5_000, [(ORG, 5_000)])  # full day-2 budget available
        assert engine.used(s, ORG) == 5_000
        assert engine.used(s, ORG, DAY1) == 3_000
        assert s.execute(select(Reservation.period).where(Reservation.request_id == "r2")).scalar_one() == DAY2
        assert engine.check_invariants(s) == []


def test_release_and_refund_after_midnight_hit_original_period(db, engine, clock):
    _add_requests(db, "rel", "ref")
    with db.transaction() as s:
        engine.reserve(s, "rel", 1_000, [(ORG, 5_000)])
        engine.reserve(s, "ref", 1_000, [(ORG, 5_000)])
        engine.settle(s, "ref")
    clock.advance(hours=20)
    with db.transaction() as s:
        engine.release(s, "rel")
        engine.refund(s, "ref", 250)
        d1 = engine.usage(s, ORG, 5_000, period=DAY1)
        assert (d1.reserved_minor, d1.settled_minor, d1.refunded_minor) == (0, 1_000, 250)
        assert s.execute(select(BudgetCounter).where(BudgetCounter.period == DAY2)).first() is None
        assert engine.check_invariants(s) == []


def test_lowered_policy_limit_applies_to_next_reservation(db, engine):
    _add_requests(db, "r1", "r2", "r3", "r4")
    with db.transaction() as s:
        engine.reserve(s, "r1", 3_000, [(ORG, 10_000)])
    with db.transaction() as s:  # policy edited: org budget lowered to 3,500
        with pytest.raises(BudgetExhausted):
            engine.reserve(s, "r2", 1_000, [(ORG, 3_500)])
        engine.reserve(s, "r3", 500, [(ORG, 3_500)])
    with db.transaction() as s:  # lowered below what is already used: available goes negative, nothing fits
        u = engine.usage(s, ORG, 2_000)
        assert u.available_minor == -1_500
        assert u.available_minor + u.reserved_minor + u.net_settled_minor == u.limit_minor
        with pytest.raises(BudgetExhausted):
            engine.reserve(s, "r4", 1, [(ORG, 2_000)])
    with db.transaction() as s:  # raised again
        engine.reserve(s, "r4", 6_500, [(ORG, 10_000)])
        assert engine.usage(s, ORG, 10_000).available_minor == 0
        assert engine.check_invariants(s) == []


# ---------------------------------------------------------------- invariant checker
def _seed_mixed_history(db, engine):
    _add_requests(db, "a", "b", "c")
    with db.transaction() as s:
        engine.reserve(s, "a", 1_000, _scopes())
        engine.reserve(s, "b", 500, _scopes())
        engine.reserve(s, "c", 300, _scopes())
        engine.settle(s, "a", 800)
        engine.refund(s, "a", 100)
        engine.release(s, "b")


def test_check_invariants_clean_after_mixed_history(db, engine):
    _seed_mixed_history(db, engine)
    with db.transaction() as s:
        assert engine.check_invariants(s) == []
        u = engine.usage(s, ORG, 10_000)
        assert (u.reserved_minor, u.settled_minor, u.refunded_minor, u.available_minor) == (300, 800, 100, 9_000)


def test_check_invariants_detects_corrupted_counter(db, engine):
    _seed_mixed_history(db, engine)
    with db.transaction() as s:
        s.execute(update(BudgetCounter).where(BudgetCounter.scope_key == ORG)
                  .values(reserved_minor=BudgetCounter.reserved_minor + 1))
    with db.transaction() as s:
        problems = engine.check_invariants(s)
        assert problems and all(p.startswith(f"{ORG}/{DAY1}") for p in problems)
        assert any("active reservations" in p for p in problems)
        assert any("reserve-release-settle" in p for p in problems)


def test_check_invariants_detects_corrupted_settled_and_refunded(db, engine):
    _seed_mixed_history(db, engine)
    with db.transaction() as s:
        s.execute(update(BudgetCounter).where(BudgetCounter.scope_key == USER)
                  .values(settled_minor=1, refunded_minor=5))
    with db.transaction() as s:
        problems = engine.check_invariants(s)
        assert any("settled 1 != ledger" in p for p in problems)
        assert any("refunded 5 != ledger" in p for p in problems)
        assert any("negative balance" in p for p in problems)


def test_check_invariants_detects_ledger_tampering_and_orphans(db, engine):
    _seed_mixed_history(db, engine)
    with db.transaction() as s:
        s.execute(delete(BudgetLedgerEntry).where(BudgetLedgerEntry.kind == "REFUND",
                                                  BudgetLedgerEntry.scope_key == ORG))
        s.add(BudgetLedgerEntry(scope_key="agent:ghost", period=DAY1, request_id="x", kind="RESERVE",
                                amount_minor=5))
    with db.transaction() as s:
        problems = engine.check_invariants(s)
        assert any(p.startswith(f"{ORG}/{DAY1}: refunded") for p in problems)
        assert any("agent:ghost" in p and "without a budget counter" in p for p in problems)


def test_check_invariants_detects_bad_reservation_rows(db, engine):
    _seed_mixed_history(db, engine)
    with db.transaction() as s:
        s.execute(update(Reservation).where(Reservation.request_id == "b", Reservation.scope_key == ORG)
                  .values(refunded_minor=10))
    with db.transaction() as s:
        problems = engine.check_invariants(s)
        assert any("RELEASED" in p and "refunded 10" in p for p in problems)
