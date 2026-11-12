"""#5 Tamper-evident audit log (hash chain)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pact_core.audit import EventType, append, to_dict, verify_chain
from pact_core.crypto import GENESIS_HASH, canonical_json, sha256_hex
from pact_core.models import AuditEvent, AuditHead, BudgetCounter


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


def _append_n(db, clock, n: int) -> None:
    for i in range(n):
        with db.transaction() as s:
            append(
                s,
                clock,
                EventType.REQUEST_CREATED,
                actor=f"agent:ag_{i}",
                request_id=f"req_{i}",
                data={"amount_minor": 100 * (i + 1), "currency": "USD", "i": i},
            )
        clock.advance(seconds=1)


def _events(db) -> list[AuditEvent]:
    with db.transaction() as s:
        return list(s.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars())


def _raw(db, sql: str, **params) -> None:
    """Tamper with storage directly, bypassing the ORM, in a separate committed transaction."""
    with db.engine.begin() as conn:
        conn.execute(text(sql), params)


def _verify(db) -> dict:
    with db.transaction() as s:
        return verify_chain(s)


# --- valid chain ---------------------------------------------------------------------------------


def test_empty_db_is_valid(db):
    result = _verify(db)
    assert result == {
        "valid": True,
        "events_checked": 0,
        "broken_at_seq": None,
        "reason": None,
        "head_seq": 0,
        "head_hash": GENESIS_HASH,
    }


def test_first_append_creates_head_from_genesis(db, clock):
    with db.transaction() as s:
        ev = append(s, clock, "REQUEST_CREATED", actor="agent:a")
    assert ev.seq == 1
    assert ev.prev_hash == GENESIS_HASH
    with db.transaction() as s:
        head = s.get(AuditHead, 1)
        assert (head.last_seq, head.last_hash) == (1, ev.hash)


def test_valid_chain_after_n_appends(db, clock):
    n = 12
    _append_n(db, clock, n)
    events = _events(db)

    assert [e.seq for e in events] == list(range(1, n + 1))
    assert events[0].prev_hash == GENESIS_HASH
    for prev, cur in zip(events, events[1:]):
        assert cur.prev_hash == prev.hash
    assert len({e.hash for e in events}) == n

    result = _verify(db)
    assert result["valid"] is True
    assert result["events_checked"] == n
    assert result["broken_at_seq"] is None
    assert result["head_seq"] == n
    assert result["head_hash"] == events[-1].hash


def test_hash_formula_matches_spec(db, clock):
    """hash = sha256(prev_hash || canonical_json({seq, ts, event_type, actor, request_id, data}))."""
    with db.transaction() as s:
        append(s, clock, "APPROVED", actor="user:u1", request_id="req_1", data={"b": 2, "a": 1})
    (ev,) = _events(db)
    body = canonical_json(
        {
            "seq": 1,
            "ts": "2026-11-12T09:00:00.000000",
            "event_type": "APPROVED",
            "actor": "user:u1",
            "request_id": "req_1",
            "data": {"a": 1, "b": 2},
        }
    )
    assert ev.hash == sha256_hex(GENESIS_HASH + body)


def test_several_appends_in_one_transaction(db, clock):
    with db.transaction() as s:
        evs = [append(s, clock, "POLICY_EVALUATED", actor="system", data={"k": k}) for k in range(5)]
    assert [e.seq for e in evs] == [1, 2, 3, 4, 5]
    assert _verify(db)["events_checked"] == 5
    assert _verify(db)["valid"] is True


def test_event_type_enum_covers_issue_minimum():
    required = {
        "REQUEST_CREATED", "POLICY_EVALUATED", "BUDGET_RESERVED", "BUDGET_RESERVATION_FAILED",
        "APPROVAL_REQUESTED", "APPROVED", "DENIED", "GRANT_ISSUED", "GRANT_DELIVERED", "GRANT_CONSUMED",
        "GRANT_REVOKED", "GRANT_EXPIRED", "EXECUTION_REJECTED", "REPLAY_REJECTED", "PROVIDER_ORDER_CREATED",
        "PROVIDER_TIMEOUT", "PAYMENT_CAPTURED", "EXECUTION_FAILED", "RECONCILED", "POLICY_UPDATED",
    }
    assert required <= {e.value for e in EventType}


def test_enum_and_plain_string_event_types_hash_identically(db, clock):
    with db.transaction() as s:
        a = append(s, clock, EventType.GRANT_ISSUED, actor="system")
    with db.transaction() as s:
        s.execute(text("DELETE FROM pact_audit_events"))
        s.execute(text("DELETE FROM pact_audit_head"))
    with db.transaction() as s:
        b = append(s, clock, "GRANT_ISSUED", actor="system")
    assert a.hash == b.hash
    assert type(a.event_type) is str


# --- timestamp / data canonicalisation -----------------------------------------------------------


@pytest.mark.parametrize(
    "ts",
    [
        datetime(2026, 11, 12, 9, 0, 0),  # microsecond == 0
        datetime(2026, 11, 12, 9, 0, 0, 123456),
        datetime(2026, 11, 12, 9, 0, 0, 1),
        datetime(2026, 11, 12, 9, 0, 0, 500000),
    ],
)
def test_timestamp_round_trip_is_stable(db, clock, ts):
    clock.now = ts
    with db.transaction() as s:
        written = append(s, clock, "REQUEST_CREATED", actor="agent:a")
    (stored,) = _events(db)
    assert stored.ts == ts
    assert stored.hash == written.hash
    assert _verify(db)["valid"] is True


def test_timezone_aware_clock_is_normalised_to_naive_utc(db, clock):
    """SQLite drops tzinfo, so an aware timestamp would hash differently once read back."""
    clock.now = datetime(2026, 11, 12, 11, 30, 0, 250000, tzinfo=timezone(timedelta(hours=2)))
    with db.transaction() as s:
        written = append(s, clock, "REQUEST_CREATED", actor="agent:a")
    assert written.ts == datetime(2026, 11, 12, 9, 30, 0, 250000)
    assert written.ts.tzinfo is None
    (stored,) = _events(db)
    assert stored.ts == written.ts
    assert _verify(db)["valid"] is True


def test_non_json_native_data_is_canonicalised_before_hashing(db, clock):
    when = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    with db.transaction() as s:
        written = append(
            s,
            clock,
            "BUDGET_RESERVED",
            actor="system",
            request_id="req_1",
            data={"at": when, "amount": Decimal("10.50"), "nested": {"z": [1, Decimal("2")]}, "name": "café"},
        )
    expected = {"at": str(when), "amount": "10.50", "nested": {"z": [1, "2"]}, "name": "café"}
    assert written.data == expected
    (stored,) = _events(db)
    assert stored.data == expected
    assert stored.hash == written.hash
    assert _verify(db)["valid"] is True


def test_none_data_is_stored_as_empty_object(db, clock):
    with db.transaction() as s:
        append(s, clock, "RECONCILED", actor="system")
    (stored,) = _events(db)
    assert stored.data == {}
    assert stored.request_id is None


def test_non_finite_floats_are_rejected(db, clock):
    with pytest.raises(ValueError):
        with db.transaction() as s:
            append(s, clock, "RECONCILED", actor="system", data={"x": float("nan")})
    assert _events(db) == []


def test_non_string_identifiers_are_coerced(db, clock):
    """A String column would store 7 as '7'; the digest must be over what is stored."""
    with db.transaction() as s:
        written = append(s, clock, "RECONCILED", actor=7, request_id=42)  # type: ignore[arg-type]
    assert (written.actor, written.request_id) == ("7", "42")
    assert _verify(db)["valid"] is True


# --- tampering -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column, value",
    [
        ("data", json.dumps({"amount_minor": 999999, "currency": "USD", "i": 2})),
        ("event_type", "APPROVED"),
        ("actor", "user:mallory"),
        ("ts", "2026-11-12 08:00:00.000000"),
        ("ts", "2026-11-12 09:00:02.000001"),
        ("request_id", "req_other"),
        ("request_id", None),
        ("hash", "f" * 64),
        ("prev_hash", "e" * 64),
    ],
)
def test_editing_any_field_breaks_chain_at_that_seq(db, clock, column, value):
    _append_n(db, clock, 5)
    assert _verify(db)["valid"] is True
    _raw(db, f"UPDATE pact_audit_events SET {column} = :v WHERE seq = 3", v=value)

    result = _verify(db)
    assert result["valid"] is False
    assert result["broken_at_seq"] == 3
    assert result["reason"] in {"hash mismatch", "prev_hash mismatch"}


def test_editing_first_and_last_event(db, clock):
    _append_n(db, clock, 4)
    _raw(db, "UPDATE pact_audit_events SET actor = 'x' WHERE seq = 1")
    assert _verify(db)["broken_at_seq"] == 1

    _raw(db, "UPDATE pact_audit_events SET actor = 'agent:ag_0' WHERE seq = 1")
    assert _verify(db)["valid"] is True  # restoring the original value restores validity

    _raw(db, "UPDATE pact_audit_events SET data = '{}' WHERE seq = 4")
    result = _verify(db)
    assert (result["valid"], result["broken_at_seq"]) == (False, 4)


def test_reordering_data_keys_is_not_tampering(db, clock):
    """Verification is over canonical JSON, so a storage-level re-serialisation is not a false alarm."""
    _append_n(db, clock, 3)
    _raw(db, """UPDATE pact_audit_events SET data = '{"i": 1, "currency": "USD", "amount_minor": 200}' WHERE seq = 2""")
    assert _verify(db)["valid"] is True


def test_rewriting_event_and_its_own_hash_is_detected_at_next_event(db, clock):
    _append_n(db, clock, 5)
    ev = _events(db)[2]
    forged_data = {"amount_minor": 1, "currency": "USD", "i": 2}
    forged_hash = sha256_hex(
        ev.prev_hash
        + canonical_json(
            {
                "seq": ev.seq,
                "ts": ev.ts.isoformat(timespec="microseconds"),
                "event_type": ev.event_type,
                "actor": ev.actor,
                "request_id": ev.request_id,
                "data": forged_data,
            }
        )
    )
    _raw(
        db,
        "UPDATE pact_audit_events SET data = :d, hash = :h WHERE seq = 3",
        d=json.dumps(forged_data),
        h=forged_hash,
    )
    result = _verify(db)
    assert result["valid"] is False
    assert result["broken_at_seq"] == 4
    assert result["reason"] == "prev_hash mismatch"


def test_deleting_middle_event_is_detected(db, clock):
    _append_n(db, clock, 5)
    _raw(db, "DELETE FROM pact_audit_events WHERE seq = 3")
    result = _verify(db)
    assert result["valid"] is False
    assert result["broken_at_seq"] == 3
    assert result["reason"] == "sequence gap"


def test_deleting_first_event_is_detected(db, clock):
    _append_n(db, clock, 3)
    _raw(db, "DELETE FROM pact_audit_events WHERE seq = 1")
    result = _verify(db)
    assert (result["valid"], result["broken_at_seq"]) == (False, 1)


def test_deleting_last_event_is_detected_by_head(db, clock):
    _append_n(db, clock, 5)
    _raw(db, "DELETE FROM pact_audit_events WHERE seq = 5")
    result = _verify(db)
    assert result["valid"] is False
    assert result["reason"] == "head mismatch"
    assert result["broken_at_seq"] == 5
    assert result["events_checked"] == 4


def test_deleting_all_events_is_detected_by_head(db, clock):
    _append_n(db, clock, 3)
    _raw(db, "DELETE FROM pact_audit_events")
    result = _verify(db)
    assert (result["valid"], result["reason"], result["broken_at_seq"]) == (False, "head mismatch", 1)


def test_deleting_head_row_is_detected(db, clock):
    _append_n(db, clock, 3)
    _raw(db, "DELETE FROM pact_audit_head")
    result = _verify(db)
    assert (result["valid"], result["reason"]) == (False, "missing head")


def test_tampering_head_seq_is_detected(db, clock):
    _append_n(db, clock, 3)
    _raw(db, "UPDATE pact_audit_head SET last_seq = 7")
    result = _verify(db)
    assert (result["valid"], result["reason"]) == (False, "head mismatch")


def test_appending_forged_event_without_head_is_detected(db, clock):
    _append_n(db, clock, 3)
    last = _events(db)[-1]
    _raw(
        db,
        "INSERT INTO pact_audit_events (seq, ts, event_type, actor, request_id, data, prev_hash, hash)"
        " VALUES (4, '2026-11-12 10:00:00.000000', 'APPROVED', 'x', NULL, '{}', :p, :h)",
        p=last.hash,
        h="a" * 64,
    )
    assert _verify(db)["broken_at_seq"] == 4


def test_truncation_with_head_rollback_needs_external_anchor(db, clock):
    """Known limitation (documented in the module): deleting the tail AND rewinding the head to the
    previous event is internally consistent. Only an externally anchored head_hash reveals it."""
    _append_n(db, clock, 4)
    anchored = _verify(db)["head_hash"]
    third = _events(db)[2]
    _raw(db, "DELETE FROM pact_audit_events WHERE seq = 4")
    _raw(db, "UPDATE pact_audit_head SET last_seq = 3, last_hash = :h", h=third.hash)

    result = _verify(db)
    assert result["valid"] is True  # undetectable from inside the database ...
    assert result["head_hash"] != anchored  # ... but an external anchor catches it


# --- transactions and concurrency ----------------------------------------------------------------


def test_append_rolls_back_with_enclosing_transaction_on_empty_db(db, clock):
    with pytest.raises(RuntimeError):
        with db.transaction() as s:
            s.add(BudgetCounter(scope_key="org:global", period="2026-11-12", reserved_minor=10))
            append(s, clock, "BUDGET_RESERVED", actor="system", data={"amount_minor": 10})
            raise RuntimeError("boom")
    with db.transaction() as s:
        assert s.execute(select(AuditEvent)).first() is None
        assert s.get(AuditHead, 1) is None
        assert s.execute(select(BudgetCounter)).first() is None
    assert _verify(db)["valid"] is True


def test_append_rolls_back_without_moving_head(db, clock):
    _append_n(db, clock, 2)
    before = _verify(db)
    with pytest.raises(RuntimeError):
        with db.transaction() as s:
            append(s, clock, "GRANT_ISSUED", actor="system")
            append(s, clock, "GRANT_DELIVERED", actor="system")
            raise RuntimeError("boom")
    after = _verify(db)
    assert after == before
    assert len(_events(db)) == 2
    with db.transaction() as s:
        assert append(s, clock, "GRANT_ISSUED", actor="system").seq == 3
    assert _verify(db)["valid"] is True


def test_stale_session_reused_across_transactions_does_not_fork(db, clock):
    """A session kept across transactions (expire_on_commit=False) must re-read the locked head."""
    session = Session(db.engine, expire_on_commit=False)
    try:
        with session.begin():
            assert append(session, clock, "REQUEST_CREATED", actor="a").seq == 1
        with db.transaction() as other:
            assert append(other, clock, "REQUEST_CREATED", actor="b").seq == 2
        with session.begin():
            assert append(session, clock, "REQUEST_CREATED", actor="a").seq == 3
    finally:
        session.close()
    result = _verify(db)
    assert (result["valid"], result["events_checked"]) == (True, 3)


def test_concurrent_appends_never_fork(db, clock):
    """Threads race on an empty DB (so the head creation races too); the chain stays linear."""
    threads, per_thread = 8, 15

    def worker(i):
        for j in range(per_thread):
            with db.transaction() as s:
                append(s, clock, "POLICY_EVALUATED", actor=f"agent:{i}", data={"thread": i, "j": j})

    assert _run_threads(threads, worker) == []

    events = _events(db)
    assert len(events) == threads * per_thread
    assert [e.seq for e in events] == list(range(1, threads * per_thread + 1))
    assert len({e.prev_hash for e in events}) == len(events)  # no two events share a parent
    for i in range(threads):
        assert [e.data["j"] for e in events if e.data["thread"] == i] == list(range(per_thread))
    result = _verify(db)
    assert result["valid"] is True
    assert result["events_checked"] == threads * per_thread
    assert result["head_hash"] == events[-1].hash


# --- serialisation -------------------------------------------------------------------------------


def test_to_dict_shape(db, clock):
    clock.now = datetime(2026, 11, 12, 9, 0, 0, 123456)
    with db.transaction() as s:
        append(s, clock, "GRANT_CONSUMED", actor="agent:a", request_id="req_1", data={"amount_minor": 5})
    (ev,) = _events(db)
    d = to_dict(ev)
    assert d == {
        "seq": 1,
        "ts": "2026-11-12T09:00:00.123456Z",
        "event_type": "GRANT_CONSUMED",
        "actor": "agent:a",
        "request_id": "req_1",
        "data": {"amount_minor": 5},
        "hash": ev.hash,
        "prev_hash": GENESIS_HASH,
    }
    json.dumps(d)  # JSON-serialisable as-is


def test_to_dict_is_externally_verifiable(db, clock):
    """An auditor holding only to_dict() output can recompute every hash."""
    _append_n(db, clock, 4)
    prev = GENESIS_HASH
    for d in (to_dict(e) for e in _events(db)):
        assert d["ts"].endswith("Z")
        body = {k: d[k] for k in ("seq", "event_type", "actor", "request_id", "data")}
        body["ts"] = d["ts"][:-1]
        assert d["prev_hash"] == prev
        assert d["hash"] == sha256_hex(prev + canonical_json(body))
        prev = d["hash"]
