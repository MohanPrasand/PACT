"""Deterministic policy engine.

``evaluate`` is a pure function of (facts, policies). It never touches the database, the network
or an LLM. Every applicable restriction produces a Finding; the final decision is:

    any DENY              -> DENY
    else any REQUIRE_APPROVAL -> REQUIRE_APPROVAL
    else                  -> ALLOW

Missing policy documents fail closed (DENY), as does any exception raised while evaluating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from pact_core.crypto import canonical_json, sha256_hex
from pact_core.policy.rules import AgentPolicy, MerchantPolicy, OrgPolicy, SystemPolicy, UserPolicy


class Effect(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class Finding(BaseModel):
    source: str  # system | org | user | merchant | agent | budget
    policy_id: str
    rule: str
    effect: Effect
    code: str
    message: str
    limit_minor: int | None = None
    observed_minor: int | None = None


class PolicyRef(BaseModel):
    id: str
    version: int


class PolicyDecision(BaseModel):
    decision: Effect
    reason_codes: list[str]
    findings: list[Finding]
    policies_evaluated: list[PolicyRef]
    applicable_limits: dict[str, int | None]
    approval_required: bool
    approval_reasons: list[str]
    policy_version: str


@dataclass(frozen=True)
class OrderFacts:
    """Authoritative order facts, fetched by PACT from the merchant system (never from the agent)."""

    order_id: str
    merchant_id: str
    amount_minor: int
    currency: str
    status: str
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationInput:
    agent_id: str
    agent_active: bool
    user_id: str
    action_type: str
    merchant_id: str
    order_id: str
    amount_minor: int
    currency: str
    merchant_exists: bool
    order: OrderFacts | None
    # Budget usage (reserved + net settled) for the current period, keyed by budget scope.
    budget_used_minor: dict[str, int] = field(default_factory=dict)
    order_has_active_authorization: bool = False
    similar_recent_payments: int = 0


@dataclass(frozen=True)
class PolicySnapshot:
    system: tuple[str, int, SystemPolicy] | None
    org: tuple[str, int, OrgPolicy] | None
    user: tuple[str, int, UserPolicy] | None
    merchant: tuple[str, int, MerchantPolicy] | None
    agent: tuple[str, int, AgentPolicy] | None

    def refs(self) -> list[PolicyRef]:
        return [PolicyRef(id=p[0], version=p[1]) for p in (self.system, self.org, self.user, self.merchant, self.agent) if p]

    def version(self) -> str:
        parts = [(p[0], p[1], p[2].model_dump()) for p in (self.system, self.org, self.user, self.merchant, self.agent) if p]
        return sha256_hex(canonical_json(parts))[:16]


def budget_scopes(snapshot: PolicySnapshot, agent_id: str, user_id: str) -> list[tuple[str, int]]:
    """The shared budgets a request draws from: (scope_key, limit). All must have headroom."""
    scopes: list[tuple[str, int]] = []
    if snapshot.org:
        scopes.append(("org:global", snapshot.org[2].daily_budget_minor))
    if snapshot.user:
        scopes.append((f"user:{user_id}", snapshot.user[2].daily_limit_minor))
    if snapshot.agent:
        scopes.append((f"agent:{agent_id}", snapshot.agent[2].daily_limit_minor))
    return scopes


def _fmt(minor: int | None, currency: str) -> str:
    if minor is None:
        return "n/a"
    return f"{minor / 100:,.2f} {currency}"


def evaluate(facts: EvaluationInput, policies: PolicySnapshot) -> PolicyDecision:
    try:
        return _evaluate(facts, policies)
    except Exception as exc:  # fail closed
        finding = Finding(
            source="system",
            policy_id="system",
            rule="evaluation",
            effect=Effect.DENY,
            code="POLICY_EVALUATION_ERROR",
            message=f"Policy evaluation failed: {type(exc).__name__}",
        )
        return PolicyDecision(
            decision=Effect.DENY,
            reason_codes=[finding.code],
            findings=[finding],
            policies_evaluated=[],
            applicable_limits={},
            approval_required=False,
            approval_reasons=[],
            policy_version="error",
        )


def _evaluate(f: EvaluationInput, p: PolicySnapshot) -> PolicyDecision:
    findings: list[Finding] = []
    cur = f.currency
    amt = f.amount_minor

    def add(source: str, policy_id: str, rule: str, effect: Effect, code: str, message: str, limit=None, observed=None):
        findings.append(
            Finding(
                source=source, policy_id=policy_id, rule=rule, effect=effect, code=code,
                message=message, limit_minor=limit, observed_minor=observed,
            )
        )

    # ---- fail closed on missing policy documents -------------------------------------------
    for name, doc in (("system", p.system), ("org", p.org), ("user", p.user), ("agent", p.agent)):
        if doc is None:
            add(name, f"{name}:?", "presence", Effect.DENY, f"{name.upper()}_POLICY_MISSING",
                f"No {name} policy is configured; failing closed.")

    # ---- system (hard) ---------------------------------------------------------------------
    if p.system:
        sid, _, sp = p.system
        if amt <= 0:
            add("system", sid, "amount", Effect.DENY, "INVALID_AMOUNT", "Amount must be positive.", observed=amt)
        if cur.upper() != sp.currency.upper():
            add("system", sid, "currency", Effect.DENY, "CURRENCY_NOT_SUPPORTED",
                f"Currency {cur} is not supported; this deployment settles in {sp.currency}.")
        if f.action_type not in sp.allowed_actions:
            add("system", sid, "allowed_actions", Effect.DENY, "ACTION_NOT_ALLOWED",
                f"Action {f.action_type} is not permitted.")
        if amt > sp.hard_max_transaction_minor:
            add("system", sid, "hard_max_transaction", Effect.DENY, "SYSTEM_TRANSACTION_LIMIT",
                f"{_fmt(amt, cur)} exceeds the system hard cap of {_fmt(sp.hard_max_transaction_minor, cur)}.",
                sp.hard_max_transaction_minor, amt)
        if not f.agent_active:
            add("system", sid, "agent_identity", Effect.DENY, "AGENT_INACTIVE", "Agent is disabled.")
        if not f.merchant_exists:
            add("system", sid, "merchant_identity", Effect.DENY, "MERCHANT_UNKNOWN",
                f"Merchant {f.merchant_id} is not a known merchant.")
        if f.order is None:
            add("system", sid, "order_binding", Effect.DENY, "ORDER_NOT_FOUND", f"Order {f.order_id} does not exist.")
        else:
            o = f.order
            mismatches = []
            if o.merchant_id != f.merchant_id:
                mismatches.append("merchant")
            if o.amount_minor != amt:
                mismatches.append("amount")
            if o.currency.upper() != cur.upper():
                mismatches.append("currency")
            if mismatches:
                add("system", sid, "order_binding", Effect.DENY, "ORDER_MISMATCH",
                    f"Requested {', '.join(mismatches)} does not match merchant order {o.order_id} "
                    f"({_fmt(o.amount_minor, o.currency)} at {o.merchant_id}).")
            if o.status not in ("CREATED", "PENDING", "FAILED", "CANCELLED"):
                add("system", sid, "order_state", Effect.DENY, "ORDER_NOT_PAYABLE",
                    f"Order {o.order_id} is in status {o.status} and cannot be paid.")
        if f.order_has_active_authorization:
            add("system", sid, "duplicate_execution", Effect.DENY, "DUPLICATE_ORDER_AUTHORIZATION",
                f"Order {f.order_id} already has an active or completed authorization.")

    # ---- organisation ----------------------------------------------------------------------
    if p.org:
        oid, _, op = p.org
        if f.merchant_id not in op.approved_merchants:
            add("org", oid, "approved_merchants", Effect.DENY, "MERCHANT_NOT_APPROVED",
                f"Merchant {f.merchant_id} is not on the organisation's approved list.")
        if op.max_transaction_minor is not None and amt > op.max_transaction_minor:
            add("org", oid, "max_transaction", Effect.DENY, "ORG_TRANSACTION_LIMIT",
                f"{_fmt(amt, cur)} exceeds the organisation per-transaction limit.", op.max_transaction_minor, amt)
        if op.duplicate_window_minutes > 0 and f.similar_recent_payments > 0:
            add("org", oid, "duplicate_window", Effect(op.duplicate_effect), "POSSIBLE_DUPLICATE_PAYMENT",
                f"{f.similar_recent_payments} payment(s) of the same amount to the same merchant in the last "
                f"{op.duplicate_window_minutes} minutes.")

    # ---- user ------------------------------------------------------------------------------
    if p.user:
        uid, _, up = p.user
        if up.max_transaction_minor is not None and amt > up.max_transaction_minor:
            add("user", uid, "max_transaction", Effect.DENY, "USER_TRANSACTION_LIMIT",
                f"{_fmt(amt, cur)} exceeds the user's per-transaction limit.", up.max_transaction_minor, amt)
        if amt > up.approval_threshold_minor:
            add("user", uid, "approval_threshold", Effect.REQUIRE_APPROVAL, "USER_APPROVAL_THRESHOLD",
                f"{_fmt(amt, cur)} is above the user's approval threshold of {_fmt(up.approval_threshold_minor, cur)}.",
                up.approval_threshold_minor, amt)

    # ---- merchant --------------------------------------------------------------------------
    if p.merchant:
        mid, _, mp = p.merchant
        if mp.max_transaction_minor is not None and amt > mp.max_transaction_minor:
            add("merchant", mid, "max_transaction", Effect.DENY, "MERCHANT_TRANSACTION_LIMIT",
                f"{_fmt(amt, cur)} exceeds the merchant's maximum agent purchase of {_fmt(mp.max_transaction_minor, cur)}.",
                mp.max_transaction_minor, amt)
        if mp.allowed_agents is not None and f.agent_id not in mp.allowed_agents:
            add("merchant", mid, "allowed_agents", Effect.DENY, "MERCHANT_AGENT_NOT_ALLOWED",
                f"Merchant does not accept purchases from agent {f.agent_id}.")
        if mp.allowed_categories is not None and f.order is not None:
            bad = sorted(set(f.order.categories) - set(mp.allowed_categories))
            if bad:
                add("merchant", mid, "allowed_categories", Effect.DENY, "MERCHANT_CATEGORY_NOT_ALLOWED",
                    f"Merchant does not allow agent purchases in: {', '.join(bad)}.")
        if mp.approval_required_above_minor is not None and amt > mp.approval_required_above_minor:
            add("merchant", mid, "approval_required_above", Effect.REQUIRE_APPROVAL, "MERCHANT_APPROVAL_REQUIRED",
                f"Merchant requires human approval above {_fmt(mp.approval_required_above_minor, cur)}.",
                mp.approval_required_above_minor, amt)

    # ---- agent -----------------------------------------------------------------------------
    if p.agent:
        aid, _, ap = p.agent
        if not ap.enabled:
            add("agent", aid, "enabled", Effect.DENY, "AGENT_POLICY_DISABLED", "Agent's spending authority is disabled.")
        if amt > ap.max_transaction_minor:
            add("agent", aid, "max_transaction", Effect.DENY, "AGENT_TRANSACTION_LIMIT",
                f"{_fmt(amt, cur)} exceeds this agent's per-transaction limit of {_fmt(ap.max_transaction_minor, cur)}.",
                ap.max_transaction_minor, amt)
        if ap.allowed_merchants is not None and f.merchant_id not in ap.allowed_merchants:
            add("agent", aid, "allowed_merchants", Effect.DENY, "AGENT_MERCHANT_NOT_ALLOWED",
                f"Agent is not authorised to buy from {f.merchant_id}.")
        if ap.approval_required_above_minor is not None and amt > ap.approval_required_above_minor:
            add("agent", aid, "approval_required_above", Effect.REQUIRE_APPROVAL, "AGENT_APPROVAL_REQUIRED",
                f"Agent purchases above {_fmt(ap.approval_required_above_minor, cur)} require approval.",
                ap.approval_required_above_minor, amt)

    # ---- shared budgets (advisory here; enforced atomically by the budget engine) ----------
    limits: dict[str, int | None] = {}
    for scope_key, limit in budget_scopes(p, f.agent_id, f.user_id):
        used = f.budget_used_minor.get(scope_key, 0)
        remaining = max(limit - used, 0)
        limits[f"budget_remaining[{scope_key}]"] = remaining
        if amt > remaining:
            add("budget", scope_key, "daily_budget", Effect.DENY, "BUDGET_EXCEEDED",
                f"{_fmt(amt, cur)} exceeds remaining {scope_key} budget of {_fmt(remaining, cur)} "
                f"(limit {_fmt(limit, cur)}, used {_fmt(used, cur)}).", limit, used + amt)

    # ---- effective per-transaction limit -----------------------------------------------------
    caps = {
        "system.hard_max_transaction": p.system[2].hard_max_transaction_minor if p.system else None,
        "org.max_transaction": p.org[2].max_transaction_minor if p.org else None,
        "user.max_transaction": p.user[2].max_transaction_minor if p.user else None,
        "merchant.max_transaction": p.merchant[2].max_transaction_minor if p.merchant else None,
        "agent.max_transaction": p.agent[2].max_transaction_minor if p.agent else None,
        "user.approval_threshold": p.user[2].approval_threshold_minor if p.user else None,
    }
    limits.update(caps)
    tx_caps = [v for k, v in caps.items() if k.endswith("max_transaction") and v is not None]
    limits["effective_max_transaction"] = min(tx_caps) if tx_caps else None

    denies = [x for x in findings if x.effect == Effect.DENY]
    approvals = [x for x in findings if x.effect == Effect.REQUIRE_APPROVAL]
    if denies:
        decision, reasons = Effect.DENY, [x.code for x in denies]
    elif approvals:
        decision, reasons = Effect.REQUIRE_APPROVAL, [x.code for x in approvals]
    else:
        decision, reasons = Effect.ALLOW, ["WITHIN_POLICY"]

    return PolicyDecision(
        decision=decision,
        reason_codes=list(dict.fromkeys(reasons)),
        findings=findings,
        policies_evaluated=p.refs(),
        applicable_limits=limits,
        approval_required=decision == Effect.REQUIRE_APPROVAL,
        approval_reasons=[x.message for x in approvals] if decision == Effect.REQUIRE_APPROVAL else [],
        policy_version=p.version(),
    )


def decision_dict(d: PolicyDecision) -> dict[str, Any]:
    return d.model_dump(mode="json")
