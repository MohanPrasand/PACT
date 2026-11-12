"""Tamper-evident audit log (hash chain).

Events are appended inside the same transaction as the state change they describe, so a decision
and its audit record commit or roll back together.

Chain
    hash(n) = sha256(hash(n-1) || canonical_json({seq, ts, event_type, actor, request_id, data}))
    with hash(0) = GENESIS_HASH ("0" * 64). ``ts`` is hashed as naive-UTC ISO-8601 with fixed
    microsecond precision (``2026-11-12T09:00:00.000000``); ``to_dict`` emits the same string plus
    ``Z``, so anyone holding exported events can recompute every hash.

Serialisation of writers
    The single ``AuditHead`` row (id=1) holds ``(last_seq, last_hash)``. ``append`` locks it with
    ``SELECT ... FOR UPDATE`` (PostgreSQL); on SQLite every transaction starts with
    ``BEGIN IMMEDIATE`` so writers are already serialised. The head row is created idempotently
    (``INSERT ... ON CONFLICT DO NOTHING``) so two first-appends racing on an empty database cannot
    fail or fork. ``seq`` is also the primary key, so a fork would surface as an integrity error.

What ``verify_chain`` detects
    Editing any field of any event (including ``hash``/``prev_hash``), deleting or inserting an
    event anywhere, appending past the head, and deleting or editing the head row.

Known limitation
    Truncating the tail *and* rewinding the head to the new last event leaves an internally
    consistent chain; no hash chain can detect that without an external anchor. ``verify_chain``
    returns ``head_seq``/``head_hash`` so the head can be periodically anchored outside the database
    (e.g. published, signed or written to append-only storage) and compared later.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from pact_core.crypto import GENESIS_HASH, sha256_hex
from pact_core.db import Clock
from pact_core.models import AuditEvent, AuditHead

_HEAD_ID = 1


class EventType(StrEnum):
    """Minimum event vocabulary (issue #5). ``append`` also accepts plain strings."""

    REQUEST_CREATED = "REQUEST_CREATED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    BUDGET_RESERVED = "BUDGET_RESERVED"
    BUDGET_RESERVATION_FAILED = "BUDGET_RESERVATION_FAILED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    GRANT_ISSUED = "GRANT_ISSUED"
    GRANT_DELIVERED = "GRANT_DELIVERED"
    GRANT_CONSUMED = "GRANT_CONSUMED"
    GRANT_REVOKED = "GRANT_REVOKED"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    EXECUTION_REJECTED = "EXECUTION_REJECTED"
    REPLAY_REJECTED = "REPLAY_REJECTED"
    PROVIDER_ORDER_CREATED = "PROVIDER_ORDER_CREATED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    RECONCILED = "RECONCILED"
    POLICY_UPDATED = "POLICY_UPDATED"
    CHECKOUT_CANCELLED_BY_BUYER = "CHECKOUT_CANCELLED_BY_BUYER"
    CHECKOUT_TIMED_OUT = "CHECKOUT_TIMED_OUT"
    CANCEL_FORCED = "CANCEL_FORCED"


def _canonical(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace, non-native values via ``str``, no NaN/Infinity
    (not valid JSON and rejected by PostgreSQL)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _naive_utc(ts: datetime) -> datetime:
    """The DB stores naive UTC; normalise aware values so what is hashed is what is read back."""
    if ts.tzinfo is not None:
        ts = ts.astimezone(UTC).replace(tzinfo=None)
    return ts


def _ts_str(ts: datetime) -> str:
    return ts.isoformat(timespec="microseconds")


def _event_digest(
    prev_hash: str, seq: int, ts: datetime, event_type: str, actor: str, request_id: str | None, data: Any
) -> str:
    body = _canonical(
        {
            "seq": seq,
            "ts": _ts_str(ts),
            "event_type": event_type,
            "actor": actor,
            "request_id": request_id,
            "data": data,
        }
    )
    return sha256_hex(prev_hash + body)


def _create_head_if_missing(session: Session) -> None:
    dialect = session.get_bind().dialect.name
    values = {"id": _HEAD_ID, "last_seq": 0, "last_hash": GENESIS_HASH}
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - other backends: plain insert, PK still prevents a second head
        session.execute(insert(AuditHead).values(**values))
        return
    session.execute(dialect_insert(AuditHead).values(**values).on_conflict_do_nothing(index_elements=["id"]))


def _lock_head(session: Session) -> AuditHead:
    stmt = (
        select(AuditHead)
        .where(AuditHead.id == _HEAD_ID)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    head = session.execute(stmt).scalar_one_or_none()
    if head is None:
        _create_head_if_missing(session)
        head = session.execute(stmt).scalar_one()
    return head


def append(
    session: Session,
    clock: Clock,
    event_type: str,
    *,
    actor: str,
    request_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    """Append one event to the chain inside the caller's transaction (no commit here)."""
    event_type = str(event_type)
    actor = str(actor)
    request_id = None if request_id is None else str(request_id)
    # Round-trip data through canonical JSON so the digest is computed over exactly what is stored
    # (datetimes, Decimals etc. become strings; tuples become lists; keys become strings).
    payload = json.loads(_canonical(data if data is not None else {}))
    head = _lock_head(session)
    ts = _naive_utc(clock())
    seq = head.last_seq + 1
    digest = _event_digest(head.last_hash, seq, ts, event_type, actor, request_id, payload)
    event = AuditEvent(
        seq=seq,
        ts=ts,
        event_type=event_type,
        actor=actor,
        request_id=request_id,
        data=payload,
        prev_hash=head.last_hash,
        hash=digest,
    )
    session.add(event)
    head.last_seq = seq
    head.last_hash = digest
    session.flush()
    return event


def _result(
    valid: bool, checked: int, head: AuditHead | None, broken_at: int | None = None, reason: str | None = None
) -> dict[str, Any]:
    return {
        "valid": valid,
        "events_checked": checked,
        "broken_at_seq": broken_at,
        "reason": reason,
        "head_seq": head.last_seq if head is not None else 0,
        "head_hash": head.last_hash if head is not None else GENESIS_HASH,
    }


def verify_chain(session: Session) -> dict[str, Any]:
    """Recompute the whole chain and check it ends at the stored head.

    Returns ``{valid, events_checked, broken_at_seq, reason, head_seq, head_hash}``.
    ``broken_at_seq`` is the first sequence number that cannot be verified (``None`` if valid);
    ``reason`` is one of ``hash mismatch``, ``prev_hash mismatch``, ``sequence gap``,
    ``head mismatch``, ``missing head`` (``None`` if valid). ``head_seq``/``head_hash`` are the
    stored head (``0``/genesis when empty), suitable for external anchoring.
    """
    # Share-lock the head first (FOR SHARE on PostgreSQL; SQLite's BEGIN IMMEDIATE already excludes
    # writers) so no append can commit between reading the head and scanning the events.
    head_stmt = (
        select(AuditHead)
        .where(AuditHead.id == _HEAD_ID)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    head = session.execute(head_stmt).scalar_one_or_none()

    prev = GENESIS_HASH
    count = 0
    events = session.execute(select(AuditEvent).order_by(AuditEvent.seq).execution_options(yield_per=500))
    for ev in events.scalars():
        count += 1
        if ev.seq != count:
            return _result(False, count, head, count, "sequence gap")
        if ev.prev_hash != prev:
            return _result(False, count, head, ev.seq, "prev_hash mismatch")
        expected = _event_digest(prev, ev.seq, ev.ts, ev.event_type, ev.actor, ev.request_id, ev.data)
        if ev.hash != expected:
            return _result(False, count, head, ev.seq, "hash mismatch")
        prev = ev.hash

    if head is None and count > 0:
        # A first-ever append may have committed after the (unlockable, absent) head was read.
        head = session.execute(head_stmt).scalar_one_or_none()
    if head is None:
        if count == 0:
            return _result(True, 0, None)
        return _result(False, count, None, count, "missing head")
    if head.last_seq != count or head.last_hash != prev:
        # Events missing from the tail (or extra/forged events beyond the head).
        return _result(False, count, head, min(count, head.last_seq) + 1, "head mismatch")
    return _result(True, count, head)


def to_dict(ev: AuditEvent) -> dict[str, Any]:
    """JSON-ready view of an event. ``ts`` is the hashed timestamp string plus ``Z`` (UTC)."""
    return {
        "seq": ev.seq,
        "ts": _ts_str(_naive_utc(ev.ts)) + "Z",
        "event_type": ev.event_type,
        "actor": ev.actor,
        "request_id": ev.request_id,
        "data": ev.data,
        "hash": ev.hash,
        "prev_hash": ev.prev_hash,
    }
