"""Shared runtime context for the authorization service (#8) and execution gateway (#10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pact_core import audit
from pact_core.budget import BudgetEngine
from pact_core.db import Clock, Database
from pact_core.errors import NotFound
from pact_core.grants import GrantManager
from pact_core.models import AuthorizationRequest, Grant, Policy, State
from pact_core.policy.engine import PolicySnapshot
from pact_core.ports import OrderSource, PaymentAdapter

PROVIDER_LABELS = {"paypal": "PayPal Sandbox", "simulation": "Simulation (not PayPal)"}


@dataclass(frozen=True)
class AgentIdentity:
    """An authenticated agent. ``user_id`` is the user the agent acts for (never self-declared)."""

    id: str
    user_id: str
    name: str = ""


@dataclass
class PactContext:
    db: Database
    clock: Clock
    orders: OrderSource
    adapters: dict[str, PaymentAdapter]
    provider: str
    currency: str
    public_url: str
    approval_ttl_seconds: int
    budget: BudgetEngine = field(init=False)
    grants: GrantManager = field(init=False)
    grant_ttl_seconds: int = 600

    def __post_init__(self) -> None:
        if self.provider not in self.adapters:
            raise ValueError(f"No adapter configured for provider {self.provider!r}")
        self.budget = BudgetEngine(self.clock)
        self.grants = GrantManager(self.clock, self.grant_ttl_seconds)
        self.public_url = self.public_url.rstrip("/")

    # ------------------------------------------------------------------ helpers
    def audit(self, s: Session, event_type: str, actor: str, request_id: str | None = None, **data: Any) -> None:
        audit.append(s, self.clock, event_type, actor=actor, request_id=request_id, data=data)

    def load_request(self, s: Session, request_id: str, *, agent: AgentIdentity | None = None) -> AuthorizationRequest:
        """Load a request. When ``agent`` is given, requests of other agents are reported as not found."""
        req = s.get(AuthorizationRequest, request_id, populate_existing=True)
        if req is None or (agent is not None and req.agent_id != agent.id):
            raise NotFound("REQUEST_NOT_FOUND", f"No authorization request {request_id}")
        return req

    def grant_for(self, s: Session, request_id: str) -> Grant | None:
        return s.execute(
            select(Grant).where(Grant.request_id == request_id).execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def snapshot(self, s: Session, *, agent_id: str, user_id: str, merchant_id: str) -> PolicySnapshot:
        """Current policy documents as raw dicts; the engine validates them and fails closed if invalid."""

        def load(policy_id: str):
            p = s.get(Policy, policy_id, populate_existing=True)
            return (p.id, p.version, dict(p.rules)) if p is not None else None

        return PolicySnapshot(
            system=load("system"),
            org=load("org:default"),
            user=load(f"user:{user_id}"),
            merchant=load(f"merchant:{merchant_id}"),
            agent=load(f"agent:{agent_id}"),
        )


# ---------------------------------------------------------------------- views


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() + "Z" if ts else None


def _money(minor: int) -> str:
    return f"{minor / 100:.2f}"


def next_action(req: AuthorizationRequest, grant: Grant | None) -> str:
    state = State(req.state)
    reasons = ", ".join((req.decision or {}).get("reason_codes", [])) or "n/a"
    if state == State.DENIED:
        return (f"Denied ({reasons}). Report this decision to the user exactly as stated; do not retry or "
                "try to work around it.")
    if state == State.PENDING_APPROVAL:
        return ("Waiting for a human to approve this exact transaction in the PACT dashboard. Poll "
                "pact_get_authorization_status. Approval cannot be given through chat or any agent tool.")
    if state == State.APPROVED:
        return "Approved by a human. Call pact_get_authorization_status to receive the single-use grant token."
    if state == State.GRANTED:
        exp = _iso(grant.expires_at) if grant else "soon"
        return (f"Call pact_execute_action with the grant_token and the exact same order, merchant, amount and "
                f"currency before {exp}. The grant works once.")
    if state == State.EXECUTING:
        if req.approval_url and req.provider_status in (None, "CREATED", "PAYER_ACTION_REQUIRED", "INSTRUMENT_DECLINED"):
            return (f"Ask the user to complete checkout at approval_url ({PROVIDER_LABELS.get(req.provider or '', req.provider)}). "
                    "PACT captures the payment after the buyer approves; then poll pact_get_authorization_status.")
        return "Payment in progress. Poll pact_get_authorization_status."
    if state == State.UNKNOWN:
        return ("The payment outcome is unknown (provider timeout). PACT is reconciling with the provider; do NOT "
                "retry the payment. Poll pact_get_authorization_status.")
    if state == State.EXECUTED:
        return f"Payment captured (capture id {req.provider_capture_id}). Tell the user the purchase is complete."
    if state == State.FAILED:
        return f"Payment failed ({req.failure_reason or 'unknown reason'}). No money was captured; the reservation was released."
    if state == State.EXPIRED:
        return "This authorization expired; its budget reservation was released. Start a new request if still needed."
    if state == State.REVOKED:
        return "This authorization was revoked; its budget reservation was released."
    return "Poll pact_get_authorization_status."


def request_view(req: AuthorizationRequest, grant: Grant | None = None, *, grant_token: str | None = None) -> dict[str, Any]:
    """Agent-safe representation of a request. Never includes the grant token hash; includes the
    plaintext token only when it is being delivered (exactly once)."""
    view: dict[str, Any] = {
        "request_id": req.id,
        "state": req.state,
        "agent_id": req.agent_id,
        "user_id": req.user_id,
        "merchant_id": req.merchant_id,
        "order_id": req.order_id,
        "action_type": req.action_type,
        "amount_minor": req.amount_minor,
        "amount": _money(req.amount_minor),
        "currency": req.currency,
        "description": req.description,
        "request_hash": req.request_hash,
        "decision": req.decision,
        "policy_version": req.policy_version,
        "approval_expires_at": _iso(req.approval_expires_at),
        "decided_by": req.decided_by,
        "decided_at": _iso(req.decided_at),
        "decision_note": req.decision_note,
        "grant": None,
        "execution": {
            "provider": req.provider,
            "provider_label": PROVIDER_LABELS.get(req.provider or "", req.provider),
            "provider_order_id": req.provider_order_id,
            "provider_status": req.provider_status,
            "capture_id": req.provider_capture_id,
            "approval_url": req.approval_url,
            "failure_reason": req.failure_reason,
        },
        "created_at": _iso(req.created_at),
        "updated_at": _iso(req.updated_at),
        "next_action": next_action(req, grant),
    }
    if grant is not None:
        view["grant"] = {
            "grant_id": grant.id,
            "expires_at": _iso(grant.expires_at),
            "consumed": grant.consumed_at is not None,
            "revoked": grant.revoked_at is not None,
        }
    if grant_token is not None:
        view["grant_token"] = grant_token
    return view
