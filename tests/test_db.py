"""#2 Persistence layer and concurrency model."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import inspect, select, update

from pact_core.models import BudgetCounter

EXPECTED_TABLES = {
    "pact_agents", "pact_policies", "pact_requests", "pact_grants", "pact_budget_counters",
    "pact_reservations", "pact_budget_ledger", "pact_audit_events", "pact_audit_head",
}


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


def test_create_all_creates_core_tables(db):
    assert EXPECTED_TABLES <= set(inspect(db.engine).get_table_names())


def test_sqlite_pragmas(db):
    with db.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_transaction_commits(db):
    with db.transaction() as s:
        s.add(BudgetCounter(scope_key="org:global", period="2026-11-12"))
    with db.transaction() as s:
        assert s.execute(select(BudgetCounter)).scalar_one().scope_key == "org:global"


def test_transaction_rolls_back_on_exception(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as s:
            s.add(BudgetCounter(scope_key="org:global", period="2026-11-12"))
            s.flush()
            raise RuntimeError("boom")
    with db.transaction() as s:
        assert s.execute(select(BudgetCounter)).first() is None


def test_begin_immediate_prevents_lost_updates(db):
    """Read-modify-write in Python is safe because every transaction takes the write lock up front."""
    with db.transaction() as s:
        s.add(BudgetCounter(scope_key="k", period="p"))

    threads, per_thread = 8, 15

    def worker(_):
        for _ in range(per_thread):
            with db.transaction() as s:
                c = s.execute(select(BudgetCounter).where(BudgetCounter.scope_key == "k")).scalar_one()
                c.reserved_minor = c.reserved_minor + 1

    assert _run_threads(threads, worker) == []
    with db.transaction() as s:
        assert s.execute(select(BudgetCounter.reserved_minor)).scalar_one() == threads * per_thread


def test_conditional_update_never_oversubscribes(db):
    """The pattern budget/grant code relies on: UPDATE ... WHERE <invariant> + rowcount check."""
    limit, amount, contenders = 50, 10, 20
    with db.transaction() as s:
        s.add(BudgetCounter(scope_key="org:global", period="p"))

    wins = []
    lock = threading.Lock()

    def worker(i):
        C = BudgetCounter
        with db.transaction() as s:
            res = s.execute(
                update(C)
                .where(C.scope_key == "org:global", (limit - C.reserved_minor) >= amount)
                .values(reserved_minor=C.reserved_minor + amount)
            )
            if res.rowcount == 1:
                with lock:
                    wins.append(i)

    assert _run_threads(contenders, worker) == []
    assert len(wins) == limit // amount
    with db.transaction() as s:
        assert s.execute(select(BudgetCounter.reserved_minor)).scalar_one() == limit
