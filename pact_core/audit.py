"""Tamper-evident audit log (hash chain). Events are appended inside the same transaction as the
state change they describe, so a decision and its audit record commit or roll back together."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pact_core.crypto import GENESIS_HASH, canonical_json, sha256_hex
from pact_core.db import Clock
from pact_core.models import AuditEvent, AuditHead


def _event_digest(prev_hash: str, seq: int, ts: str, event_type: str, actor: str, request_id: str | None, data: dict) -> str:
    body = canonical_json(
        {"seq": seq, "ts": ts, "event_type": event_type, "actor": actor, "request_id": request_id, "data": data}
    )
    return sha256_hex(prev_hash + body)


def append(
    session: Session,
    clock: Clock,
    event_type: str,
    *,
    actor: str,
    request_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent:
    head = session.execute(select(AuditHead).where(AuditHead.id == 1).with_for_update()).scalar_one_or_none()
    if head is None:
        head = AuditHead(id=1, last_seq=0, last_hash=GENESIS_HASH)
        session.add(head)
        session.flush()
    # Round-trip data through JSON so the digest is computed over exactly what is stored.
    payload = json.loads(canonical_json(data or {}))
    ts = clock()
    seq = head.last_seq + 1
    digest = _event_digest(head.last_hash, seq, ts.isoformat(), event_type, actor, request_id, payload)
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


def verify_chain(session: Session) -> dict[str, Any]:
    prev = GENESIS_HASH
    count = 0
    for ev in session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars():
        count += 1
        expected = _event_digest(prev, ev.seq, ev.ts.isoformat(), ev.event_type, ev.actor, ev.request_id, ev.data)
        if ev.prev_hash != prev or ev.hash != expected:
            return {"valid": False, "events_checked": count, "broken_at_seq": ev.seq}
        prev = ev.hash
    head = session.get(AuditHead, 1)
    if head is not None and head.last_hash != prev:
        return {"valid": False, "events_checked": count, "broken_at_seq": head.last_seq, "reason": "head mismatch"}
    return {"valid": True, "events_checked": count, "head_hash": prev}


def to_dict(ev: AuditEvent) -> dict[str, Any]:
    return {
        "seq": ev.seq,
        "ts": ev.ts.isoformat() + "Z",
        "event_type": ev.event_type,
        "actor": ev.actor,
        "request_id": ev.request_id,
        "data": ev.data,
        "hash": ev.hash,
        "prev_hash": ev.prev_hash,
    }
