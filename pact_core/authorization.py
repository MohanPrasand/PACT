"""Authorization service (#8): request → evaluate → reserve → decide, plus human approval, denial,
revocation, expiry and single-use grant delivery.

All lifecycle rules live here (and in the execution gateway). REST and MCP only call into this module.
Rule: Merchant Lab is queried *before* a transaction opens, never inside one.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pact_core.budget import BudgetExhausted
from pact_core.crypto import new_id, request_hash
from pact_core.errors import Conflict, PactError
from pact_core.models import AuthorizationRequest, State
from pact_core.policy.engine import (
    Effect,
    EvaluationInput,
    Finding,
    OrderFacts,
    PolicyDecision,
    budget_scopes,
    evaluate,
)
from pact_core.runtime import AgentIdentity, PactContext, request_view
from pact_core.state_machine import RESERVING, transition

# Requests that block another authorization for the same order / count towards duplicate detection.
ACTIVE_OR_DONE = frozenset(s.value for s in RESERVING) | {State.EXECUTED.value}


class AuthorizationInput(BaseModel):
    """What an agent submits. Amounts are integer minor units; the order itself is re-read from the merchant."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    merchant_id: str = Field(min_length=1, max_length=64)
    order_id: str = Field(min_length=1, max_length=64)
    amount_minor: StrictInt
    currency: str = Field(min_length=3, max_length=3)
    action_type: str = Field("PURCHASE", min_length=1, max_length=32)
    description: str = Field("", max_length=500)


class AuthorizationService:
    def __init__(self, ctx: PactContext):
        self.ctx = ctx

    # ------------------------------------------------------------------ evaluation
    def _facts(self, s: Session, agent: AgentIdentity, inp: AuthorizationInput, order: OrderFacts | None,
               merchant_exists: bool, snapshot, *, exclude_request: str | None = None) -> EvaluationInput:
        ctx = self.ctx
        used = {key: ctx.budget.used(s, key) for key, _ in budget_scopes(snapshot, agent.id, agent.user_id)}

        active = select(func.count()).select_from(AuthorizationRequest).where(
            AuthorizationRequest.order_id == inp.order_id, AuthorizationRequest.state.in_(ACTIVE_OR_DONE))
        if exclude_request:
            active = active.where(AuthorizationRequest.id != exclude_request)
        order_active = s.execute(active).scalar_one() > 0

        similar = 0
        window = _org_duplicate_window(snapshot)
        if window > 0:
            since = ctx.clock() - timedelta(minutes=window)
            q = select(func.count()).select_from(AuthorizationRequest).where(
                AuthorizationRequest.user_id == agent.user_id,
                AuthorizationRequest.merchant_id == inp.merchant_id,
                AuthorizationRequest.amount_minor == inp.amount_minor,
                AuthorizationRequest.order_id != inp.order_id,
                AuthorizationRequest.state.in_(ACTIVE_OR_DONE),
                AuthorizationRequest.created_at >= since,
            )
            similar = s.execute(q).scalar_one()

        return EvaluationInput(
            agent_id=agent.id,
            agent_active=True,  # inactive agents are rejected at authentication
            user_id=agent.user_id,
            action_type=inp.action_type,
            merchant_id=inp.merchant_id,
            order_id=inp.order_id,
            amount_minor=inp.amount_minor,
            currency=inp.currency.upper(),
            merchant_exists=merchant_exists,
            order=order,
            budget_used_minor=used,
            order_has_active_authorization=order_active,
            similar_recent_payments=similar,
        )

    def evaluate_policy(self, agent: AgentIdentity, inp: AuthorizationInput) -> dict[str, Any]:
        """Dry run: same evaluation as a real request, but nothing is reserved, stored or audited."""
        order = self.ctx.orders.get_order(inp.order_id)
        merchant_exists = self.ctx.orders.merchant_exists(inp.merchant_id)
        with self.ctx.db.transaction() as s:
            snapshot = self.ctx.snapshot(s, agent_id=agent.id, user_id=agent.user_id, merchant_id=inp.merchant_id)
            decision = evaluate(self._facts(s, agent, inp, order, merchant_exists, snapshot), snapshot)
        return {"dry_run": True, "decision": decision.model_dump(mode="json")}

    # ------------------------------------------------------------------ request
    def request_authorization(self, agent: AgentIdentity, inp: AuthorizationInput) -> dict[str, Any]:
        ctx = self.ctx
        order = ctx.orders.get_order(inp.order_id)
        merchant_exists = ctx.orders.merchant_exists(inp.merchant_id)
        summary = ctx.orders.order_summary(inp.order_id)  # shown to the approver; read before the transaction
        actor = f"agent:{agent.id}"
        rid = new_id("auth")
        currency = inp.currency.upper()

        with ctx.db.transaction() as s:
            now = ctx.clock()
            req = AuthorizationRequest(
                id=rid, agent_id=agent.id, user_id=agent.user_id, merchant_id=inp.merchant_id,
                order_id=inp.order_id, action_type=inp.action_type, amount_minor=inp.amount_minor,
                currency=currency, description=inp.description, state=State.CREATED.value,
                request_hash=request_hash(agent_id=agent.id, user_id=agent.user_id, merchant_id=inp.merchant_id,
                                          order_id=inp.order_id, action_type=inp.action_type,
                                          amount_minor=inp.amount_minor, currency=currency),
                order_snapshot=summary,
                created_at=now, updated_at=now,
            )
            s.add(req)
            s.flush()
            ctx.audit(s, "REQUEST_CREATED", actor, rid, merchant_id=req.merchant_id, order_id=req.order_id,
                      amount_minor=req.amount_minor, currency=currency, action_type=req.action_type,
                      request_hash=req.request_hash, description=req.description)
            transition(s, req, State.EVALUATING, ctx.clock)

            snapshot = ctx.snapshot(s, agent_id=agent.id, user_id=agent.user_id, merchant_id=inp.merchant_id)
            facts = self._facts(s, agent, inp, order, merchant_exists, snapshot, exclude_request=rid)
            decision = evaluate(facts, snapshot)

            if decision.decision != Effect.DENY:
                scopes = budget_scopes(snapshot, agent.id, agent.user_id)
                try:
                    ctx.budget.reserve(s, rid, inp.amount_minor, scopes)
                    ctx.audit(s, "BUDGET_RESERVED", "system", rid, amount_minor=inp.amount_minor,
                              scopes=[k for k, _ in scopes])
                except BudgetExhausted as exc:
                    decision = _with_reservation_failure(decision, exc.scope_key, inp.amount_minor)
                    ctx.audit(s, "BUDGET_RESERVATION_FAILED", "system", rid, scope=exc.scope_key,
                              amount_minor=inp.amount_minor)

            req.decision = decision.model_dump(mode="json")
            req.policy_version = decision.policy_version
            ctx.audit(s, "POLICY_EVALUATED", "policy-engine", rid, decision=decision.decision.value,
                      reason_codes=decision.reason_codes, policy_version=decision.policy_version,
                      policies=[p.model_dump() for p in decision.policies_evaluated])

            token = None
            grant = None
            if decision.decision == Effect.DENY:
                transition(s, req, State.DENIED, ctx.clock)
                ctx.audit(s, "DENIED", "policy-engine", rid, reason_codes=decision.reason_codes)
            elif decision.decision == Effect.REQUIRE_APPROVAL:
                transition(s, req, State.PENDING_APPROVAL, ctx.clock)
                req.approval_expires_at = ctx.clock() + timedelta(seconds=ctx.approval_ttl_seconds)
                ctx.audit(s, "APPROVAL_REQUESTED", "policy-engine", rid, reasons=decision.approval_reasons,
                          approval_expires_at=req.approval_expires_at.isoformat())
            else:
                transition(s, req, State.APPROVED, ctx.clock)
                req.decided_by, req.decided_at = "policy-engine", ctx.clock()
                ctx.audit(s, "APPROVED", "policy-engine", rid, mode="automatic", request_hash=req.request_hash)
                grant, token = self._issue_grant(s, req, actor)
            view = request_view(req, grant, grant_token=token)
        return view

    def _issue_grant(self, s: Session, req: AuthorizationRequest, actor: str):
        grant, token = self.ctx.grants.issue(s, req)
        transition(s, req, State.GRANTED, self.ctx.clock)
        self.ctx.audit(s, "GRANT_ISSUED", "system", req.id, grant_id=grant.id,
                       expires_at=grant.expires_at.isoformat(), binding={
                           "agent_id": grant.agent_id, "merchant_id": grant.merchant_id,
                           "order_id": grant.order_id, "amount_minor": grant.amount_minor,
                           "currency": grant.currency, "request_hash": grant.request_hash})
        self.ctx.audit(s, "GRANT_DELIVERED", actor, req.id, grant_id=grant.id)
        return grant, token

    # ------------------------------------------------------------------ status / grant delivery
    def get_status(self, agent: AgentIdentity, request_id: str) -> dict[str, Any]:
        """Agent view of its own request. An APPROVED request is turned into a grant on first fetch and
        the token is returned exactly once (only its hash is stored)."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id, agent=agent)
            if req.state == State.APPROVED.value:
                if req.approval_expires_at and req.approval_expires_at <= ctx.clock():
                    self._expire(s, req, "APPROVAL_CLAIM_WINDOW_ELAPSED")
                else:
                    grant, token = self._issue_grant(s, req, f"agent:{agent.id}")
                    return request_view(req, grant, grant_token=token)
            return request_view(req, ctx.grant_for(s, req.id))

    def view(self, request_id: str) -> dict[str, Any]:
        with self.ctx.db.transaction() as s:
            req = self.ctx.load_request(s, request_id)
            return request_view(req, self.ctx.grant_for(s, req.id))

    # ------------------------------------------------------------------ human decisions (admin only)
    def approve(self, admin: str, request_id: str, expected_request_hash: str, note: str = "") -> dict[str, Any]:
        """Approve the *exact* transaction the human saw. ``expected_request_hash`` must match, and the
        merchant order must be unchanged since the request was made; otherwise approval is refused."""
        ctx = self.ctx
        with ctx.db.transaction() as s:
            peek = ctx.load_request(s, request_id)
            order_id = peek.order_id
        order = ctx.orders.get_order(order_id)

        error: PactError | None = None
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state != State.PENDING_APPROVAL.value:
                raise Conflict("NOT_PENDING_APPROVAL", f"Request is {req.state}; only PENDING_APPROVAL can be approved")
            if expected_request_hash != req.request_hash:
                raise Conflict("APPROVAL_HASH_MISMATCH",
                               "The request changed since it was displayed; reload before approving")
            if req.approval_expires_at and req.approval_expires_at <= ctx.clock():
                self._expire(s, req, "APPROVAL_WINDOW_ELAPSED")
                error = Conflict("APPROVAL_EXPIRED", "The approval window has elapsed")
            elif _live_hash(req, order) != req.request_hash:
                transition(s, req, State.DENIED, ctx.clock)
                ctx.budget.release(s, req.id)
                req.decided_by, req.decided_at = f"admin:{admin}", ctx.clock()
                req.decision_note = "Merchant order changed after the request was made"
                ctx.audit(s, "DENIED", "system", req.id, reason_codes=["ORDER_MODIFIED_SINCE_REQUEST"],
                          attempted_by=f"admin:{admin}")
                error = Conflict("ORDER_MODIFIED_SINCE_REQUEST",
                                 "The merchant order changed after the request; it can no longer be approved")
            else:
                transition(s, req, State.APPROVED, ctx.clock)
                req.decided_by, req.decided_at, req.decision_note = f"admin:{admin}", ctx.clock(), note or None
                # Window for the requesting agent to claim its grant.
                req.approval_expires_at = ctx.clock() + timedelta(seconds=ctx.approval_ttl_seconds)
                ctx.audit(s, "APPROVED", f"admin:{admin}", req.id, mode="human", request_hash=req.request_hash,
                          note=note)
            view = request_view(req, ctx.grant_for(s, req.id))
        if error:
            raise error
        return view

    def deny(self, admin: str, request_id: str, reason: str = "") -> dict[str, Any]:
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id)
            if req.state != State.PENDING_APPROVAL.value:
                raise Conflict("NOT_PENDING_APPROVAL", f"Request is {req.state}; only PENDING_APPROVAL can be denied")
            transition(s, req, State.DENIED, ctx.clock)
            released = ctx.budget.release(s, req.id)
            req.decided_by, req.decided_at, req.decision_note = f"admin:{admin}", ctx.clock(), reason or None
            ctx.audit(s, "DENIED", f"admin:{admin}", req.id, reason_codes=["HUMAN_DENIED"], note=reason,
                      reservations_released=released)
            return request_view(req, ctx.grant_for(s, req.id))

    # ------------------------------------------------------------------ revocation / expiry
    def revoke(self, request_id: str, *, agent: AgentIdentity | None = None, admin: str | None = None) -> dict[str, Any]:
        """Revoke an unused authorization (pending, approved or granted). Agents may revoke only their own."""
        if (agent is None) == (admin is None):
            raise ValueError("exactly one of agent/admin is required")
        actor = f"agent:{agent.id}" if agent else f"admin:{admin}"
        ctx = self.ctx
        with ctx.db.transaction() as s:
            req = ctx.load_request(s, request_id, agent=agent)
            if req.state not in (State.PENDING_APPROVAL.value, State.APPROVED.value, State.GRANTED.value):
                raise Conflict("NOT_REVOCABLE", f"Request is {req.state}; only unused authorizations can be revoked")
            transition(s, req, State.REVOKED, ctx.clock)
            grant_revoked = ctx.grants.revoke(s, req.id, actor)
            released = ctx.budget.release(s, req.id)
            ctx.audit(s, "GRANT_REVOKED", actor, req.id, grant_revoked=grant_revoked, reservations_released=released)
            return request_view(req, ctx.grant_for(s, req.id))

    def _expire(self, s: Session, req: AuthorizationRequest, reason: str) -> None:
        transition(s, req, State.EXPIRED, self.ctx.clock)
        released = self.ctx.budget.release(s, req.id)
        req.failure_reason = reason
        self.ctx.audit(s, "GRANT_EXPIRED" if reason == "GRANT_EXPIRED" else "EXPIRED", "system", req.id,
                       reason=reason, reservations_released=released)

    def expire_stale(self) -> list[str]:
        """Expire pending/approved requests past their window and granted requests whose grant expired."""
        ctx = self.ctx
        expired: list[str] = []
        with ctx.db.transaction() as s:
            now = ctx.clock()
            stale = s.execute(
                select(AuthorizationRequest).where(
                    AuthorizationRequest.state.in_([State.PENDING_APPROVAL.value, State.APPROVED.value]),
                    AuthorizationRequest.approval_expires_at <= now,
                )
            ).scalars().all()
            for req in stale:
                reason = "APPROVAL_WINDOW_ELAPSED" if req.state == State.PENDING_APPROVAL.value \
                    else "APPROVAL_CLAIM_WINDOW_ELAPSED"
                self._expire(s, req, reason)
                expired.append(req.id)
            for grant in ctx.grants.find_expired_unconsumed(s):
                req = ctx.load_request(s, grant.request_id)
                if req.state == State.GRANTED.value:
                    self._expire(s, req, "GRANT_EXPIRED")
                    expired.append(req.id)
        return expired


# ---------------------------------------------------------------------- helpers


def _org_duplicate_window(snapshot) -> int:
    if not snapshot.org:
        return 0
    rules = snapshot.org[2]
    value = rules.get("duplicate_window_minutes", 0) if isinstance(rules, dict) else getattr(rules, "duplicate_window_minutes", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _live_hash(req: AuthorizationRequest, order: OrderFacts | None) -> str | None:
    """Request hash recomputed from the authoritative merchant order (None if the order is gone)."""
    if order is None:
        return None
    return request_hash(agent_id=req.agent_id, user_id=req.user_id, merchant_id=order.merchant_id,
                        order_id=order.order_id, action_type=req.action_type, amount_minor=order.amount_minor,
                        currency=order.currency)


def _with_reservation_failure(decision: PolicyDecision, scope_key: str, amount: int) -> PolicyDecision:
    """A budget reservation can fail even after the advisory check passed (a concurrent request took the
    headroom). That is a hard DENY: human approval can never override missing funds."""
    finding = Finding(source="budget", policy_id=scope_key, rule="atomic_reservation", effect=Effect.DENY,
                      code="BUDGET_RESERVATION_FAILED", observed_minor=amount,
                      message=f"Could not reserve {amount / 100:.2f} against {scope_key}: budget exhausted.")
    return decision.model_copy(update={
        "decision": Effect.DENY,
        "reason_codes": [finding.code],
        "findings": [*decision.findings, finding],
        "approval_required": False,
        "approval_reasons": [],
    })
