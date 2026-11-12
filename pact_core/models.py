"""Persistence models for PACT Core. Amounts are integer minor units (e.g. cents)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pact_core.db import Base, utcnow


class State(StrEnum):
    CREATED = "CREATED"
    EVALUATING = "EVALUATING"
    DENIED = "DENIED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    GRANTED = "GRANTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    UNKNOWN = "UNKNOWN"


class Agent(Base):
    __tablename__ = "pact_agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    user_id: Mapped[str] = mapped_column(String(64))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Policy(Base):
    """One policy document per (scope, subject). ``rules`` is validated by pact_core.policy.rules."""

    __tablename__ = "pact_policies"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # e.g. "agent:ag_shopping"
    scope: Mapped[str] = mapped_column(String(16))  # system | org | user | merchant | agent
    subject: Mapped[str] = mapped_column(String(64))
    rules: Mapped[dict[str, Any]] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64), default="seed")


class AuthorizationRequest(Base):
    __tablename__ = "pact_requests"
    __table_args__ = (Index("ix_req_order", "order_id"), Index("ix_req_state", "state"))

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("pact_agents.id"))
    user_id: Mapped[str] = mapped_column(String(64))
    merchant_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(32))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    description: Mapped[str] = mapped_column(Text, default="")
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default=State.CREATED)
    decision: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    order_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    approval_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_capture_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    execute_idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Grant(Base):
    """Transaction-bound, single-use execution permission. Only a hash of the token is stored."""

    __tablename__ = "pact_grants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("pact_requests.id"), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    merchant_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(32))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    request_hash: Mapped[str] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class BudgetCounter(Base):
    """Running totals for one budget scope (org/user/agent) in one period (UTC day)."""

    __tablename__ = "pact_budget_counters"
    __table_args__ = (UniqueConstraint("scope_key", "period", name="uq_budget_scope_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(96))
    period: Mapped[str] = mapped_column(String(16))
    reserved_minor: Mapped[int] = mapped_column(Integer, default=0)
    settled_minor: Mapped[int] = mapped_column(Integer, default=0)
    refunded_minor: Mapped[int] = mapped_column(Integer, default=0)


class Reservation(Base):
    __tablename__ = "pact_reservations"
    __table_args__ = (Index("ix_resv_request", "request_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("pact_requests.id"))
    scope_key: Mapped[str] = mapped_column(String(96))
    period: Mapped[str] = mapped_column(String(16))
    amount_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")  # ACTIVE | SETTLED | RELEASED
    refunded_minor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BudgetLedgerEntry(Base):
    """Append-only budget ledger. Counters are a materialised view of these entries."""

    __tablename__ = "pact_budget_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_key: Mapped[str] = mapped_column(String(96))
    period: Mapped[str] = mapped_column(String(16))
    request_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))  # RESERVE | RELEASE | SETTLE | REFUND
    amount_minor: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    """Hash-chained audit log: hash = sha256(prev_hash || canonical(event))."""

    __tablename__ = "pact_audit_events"
    __table_args__ = (Index("ix_audit_request", "request_id"),)

    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    event_type: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(80))
    request_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class AuditHead(Base):
    """Single row that serialises appends to the audit chain (locked FOR UPDATE on PostgreSQL)."""

    __tablename__ = "pact_audit_head"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    last_hash: Mapped[str] = mapped_column(String(64))
