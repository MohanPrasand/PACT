"""PactCore: the single entry point used by every interface (REST, MCP, tests).

Agent-facing operations take an authenticated ``AgentIdentity``; admin operations take an admin actor
name and must only be reachable behind admin authentication. No agent-facing operation can approve,
deny or change policy.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from pact_core import audit as audit_log
from pact_core.authorization import AuthorizationInput, AuthorizationService
from pact_core.crypto import constant_time_equal, sha256_hex
from pact_core.db import Clock, Database, utcnow
from pact_core.errors import Conflict, Forbidden, NotFound, PactError
from pact_core.gateway import ExecuteInput, ExecutionGateway
from pact_core.models import Agent, AuditEvent, AuthorizationRequest, Policy, State
from pact_core.policy.engine import budget_scopes
from pact_core.policy.rules import validate_rules
from pact_core.ports import OrderSource, PaymentAdapter
from pact_core.runtime import AgentIdentity, PactContext


class PactCore:
    def __init__(self, db: Database, orders: OrderSource, adapters: dict[str, PaymentAdapter], *,
                 provider: str, currency: str, public_url: str, grant_ttl_seconds: int = 600,
                 approval_ttl_seconds: int = 1800, clock: Clock = utcnow):
        self.ctx = PactContext(db=db, clock=clock, orders=orders, adapters=adapters, provider=provider,
                               currency=currency, public_url=public_url, approval_ttl_seconds=approval_ttl_seconds,
                               grant_ttl_seconds=grant_ttl_seconds)
        self.auth = AuthorizationService(self.ctx)
        self.gateway = ExecutionGateway(self.ctx)

    @property
    def db(self) -> Database:
        return self.ctx.db

    # ------------------------------------------------------------------ identity
    def authenticate_agent(self, api_key: str) -> AgentIdentity:
        if not api_key:
            raise Forbidden("AGENT_AUTH_REQUIRED", "Agent API key required", status_code=401)
        digest = sha256_hex(api_key)
        with self.db.transaction() as s:
            agent = s.execute(select(Agent).where(Agent.api_key_hash == digest)).scalar_one_or_none()
            if agent is None or not constant_time_equal(agent.api_key_hash, digest):
                raise Forbidden("AGENT_AUTH_INVALID", "Invalid agent API key", status_code=401)
            if not agent.active:
                raise Forbidden("AGENT_INACTIVE", "Agent is disabled")
            return AgentIdentity(id=agent.id, user_id=agent.user_id, name=agent.name)

    # ------------------------------------------------------------------ agent operations
    def request_authorization(self, agent: AgentIdentity, data: dict | AuthorizationInput) -> dict[str, Any]:
        return self.auth.request_authorization(agent, _model(AuthorizationInput, data))

    def evaluate_policy(self, agent: AgentIdentity, data: dict | AuthorizationInput) -> dict[str, Any]:
        return self.auth.evaluate_policy(agent, _model(AuthorizationInput, data))

    def get_status(self, agent: AgentIdentity, request_id: str) -> dict[str, Any]:
        view = self.auth.get_status(agent, request_id)
        if view["state"] in (State.EXECUTING.value, State.UNKNOWN.value) and "grant_token" not in view:
            self.gateway.reconcile(request_id)  # pick up a buyer approval or resolve an UNKNOWN outcome
            view = self.auth.get_status(agent, request_id)
        return view

    def execute(self, agent: AgentIdentity, data: dict | ExecuteInput) -> dict[str, Any]:
        return self.gateway.execute(agent, _model(ExecuteInput, data))

    def revoke(self, agent: AgentIdentity, request_id: str) -> dict[str, Any]:
        return self.auth.revoke(request_id, agent=agent)

    def agent_audit(self, agent: AgentIdentity, request_id: str | None = None, limit: int = 100) -> list[dict]:
        with self.db.transaction() as s:
            own = select(AuthorizationRequest.id).where(AuthorizationRequest.agent_id == agent.id)
            q = select(AuditEvent).where(AuditEvent.request_id.in_(own))
            if request_id:
                q = q.where(AuditEvent.request_id == request_id)
            rows = s.execute(q.order_by(AuditEvent.seq.desc()).limit(_limit(limit))).scalars()
            return [audit_log.to_dict(e) for e in rows]

    def budget_status(self, agent: AgentIdentity | None = None) -> dict[str, Any]:
        """Shared budget usage for the current period: org, user and (for an agent) its own cap."""
        with self.db.transaction() as s:
            agents = [agent] if agent else [
                AgentIdentity(a.id, a.user_id, a.name) for a in s.execute(select(Agent).order_by(Agent.id)).scalars()]
            seen: dict[str, dict] = {}
            for a in agents:
                snap = self.ctx.snapshot(s, agent_id=a.id, user_id=a.user_id, merchant_id="")
                for key, limit in budget_scopes(snap, a.id, a.user_id):
                    if key not in seen:
                        seen[key] = self.ctx.budget.usage(s, key, limit).as_dict()
            return {"currency": self.ctx.currency, "scopes": list(seen.values())}

    # ------------------------------------------------------------------ admin operations
    def approve(self, admin: str, request_id: str, expected_request_hash: str, note: str = "") -> dict[str, Any]:
        return self.auth.approve(admin, request_id, expected_request_hash, note)

    def deny(self, admin: str, request_id: str, reason: str = "") -> dict[str, Any]:
        return self.auth.deny(admin, request_id, reason)

    def admin_revoke(self, admin: str, request_id: str) -> dict[str, Any]:
        return self.auth.revoke(request_id, admin=admin)

    def cancel_payment(self, admin: str, request_id: str) -> dict[str, Any]:
        return self.gateway.abandon(request_id, f"admin:{admin}")

    def reconcile(self, request_id: str) -> dict[str, Any]:
        return self.gateway.reconcile(request_id)

    def expire_stale(self) -> list[str]:
        return self.auth.expire_stale()

    def list_requests(self, state: str | None = None, limit: int = 100) -> list[dict]:
        from pact_core.runtime import request_view

        with self.db.transaction() as s:
            q = select(AuthorizationRequest).order_by(AuthorizationRequest.created_at.desc()).limit(_limit(limit))
            if state:
                q = q.where(AuthorizationRequest.state == state)
            return [request_view(r, self.ctx.grant_for(s, r.id)) for r in s.execute(q).scalars()]

    def request_detail(self, request_id: str) -> dict[str, Any]:
        view = self.auth.view(request_id)
        with self.db.transaction() as s:
            req = self.ctx.load_request(s, request_id)
            view["order_snapshot"] = req.order_snapshot
            view["reservations"] = [
                {"scope": r.scope_key, "period": r.period, "amount_minor": r.amount_minor, "status": r.status}
                for r in self.ctx.budget.active_reservations(s, request_id)]
            events = s.execute(select(AuditEvent).where(AuditEvent.request_id == request_id)
                               .order_by(AuditEvent.seq)).scalars()
            view["audit"] = [audit_log.to_dict(e) for e in events]
        view["merchant_name"] = self.ctx.orders.merchant_name(view["merchant_id"])
        view["order_current"] = self.ctx.orders.order_summary(view["order_id"])
        return view

    def audit_events(self, *, request_id: str | None = None, event_type: str | None = None,
                     limit: int = 200) -> list[dict]:
        with self.db.transaction() as s:
            q = select(AuditEvent)
            if request_id:
                q = q.where(AuditEvent.request_id == request_id)
            if event_type:
                q = q.where(AuditEvent.event_type == event_type)
            return [audit_log.to_dict(e) for e in s.execute(q.order_by(AuditEvent.seq.desc()).limit(_limit(limit))).scalars()]

    def verify_audit(self) -> dict[str, Any]:
        with self.db.transaction() as s:
            return audit_log.verify_chain(s)

    def list_agents(self) -> list[dict]:
        """Agents with today's spend against their own cap. API key hashes are never exposed."""
        budgets = {s["scope"]: s for s in self.budget_status()["scopes"]}
        with self.db.transaction() as s:
            return [{"agent_id": a.id, "name": a.name, "description": a.description, "user_id": a.user_id,
                     "active": a.active, "created_at": a.created_at.isoformat() + "Z",
                     "budget": budgets.get(f"agent:{a.id}")}
                    for a in s.execute(select(Agent).order_by(Agent.id)).scalars()]

    def overview(self) -> dict[str, Any]:
        with self.db.transaction() as s:
            from sqlalchemy import func

            counts = dict(s.execute(select(AuthorizationRequest.state, func.count())
                                    .group_by(AuthorizationRequest.state)).all())
        return {"currency": self.ctx.currency, "provider": self.ctx.provider,
                "budgets": self.budget_status()["scopes"], "agents": self.list_agents(),
                "requests_by_state": counts, "pending_approvals": counts.get(State.PENDING_APPROVAL.value, 0),
                "recent": self.list_requests(limit=10), "audit": self.verify_audit()}

    def list_policies(self) -> list[dict]:
        with self.db.transaction() as s:
            return [{"id": p.id, "scope": p.scope, "subject": p.subject, "rules": p.rules, "version": p.version,
                     "updated_at": p.updated_at.isoformat() + "Z", "updated_by": p.updated_by}
                    for p in s.execute(select(Policy).order_by(Policy.id)).scalars()]

    def update_policy(self, admin: str, policy_id: str, rules: dict, expected_version: int) -> dict:
        """Replace a policy document. Validated strictly; optimistic concurrency via ``expected_version``."""
        with self.db.transaction() as s:
            p = s.get(Policy, policy_id)
            if p is None:
                raise NotFound("POLICY_NOT_FOUND", f"No policy {policy_id}")
            if p.version != expected_version:
                raise Conflict("POLICY_VERSION_CONFLICT", f"Policy is at version {p.version}, not {expected_version}")
            try:
                clean = validate_rules(p.scope, rules)
            except ValueError as exc:
                raise PactError("POLICY_INVALID", "Policy document failed validation",
                                details={"errors": _errors(exc)}) from None
            before = p.rules
            p.rules, p.version, p.updated_at, p.updated_by = clean, p.version + 1, self.ctx.clock(), f"admin:{admin}"
            self.ctx.audit(s, "POLICY_UPDATED", f"admin:{admin}", None, policy_id=p.id, version=p.version,
                           before=before, after=clean)
            return {"id": p.id, "scope": p.scope, "rules": p.rules, "version": p.version}

    # ------------------------------------------------------------------ provider callbacks
    def handle_provider_return(self, provider_order_id: str) -> dict[str, Any]:
        return self.gateway.handle_provider_return(provider_order_id)

    def handle_provider_cancel(self, provider_order_id: str) -> dict[str, Any]:
        return self.gateway.handle_provider_cancel(provider_order_id)


def _model(cls, data):
    if isinstance(data, cls):
        return data
    try:
        return cls.model_validate(data)
    except ValueError as exc:
        raise PactError("INVALID_REQUEST", "Request failed validation", details={"errors": _errors(exc)}) from None


def _errors(exc: Exception) -> list:
    errs = getattr(exc, "errors", None)
    if callable(errs):
        return [{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in errs(include_input=False)]
    return [str(exc)]


def _limit(n: int) -> int:
    return max(1, min(int(n), 500))
