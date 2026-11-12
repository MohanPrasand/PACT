"""Deterministic policy engine.

``evaluate`` is a pure function of (facts, policies). It never touches the database, the network
or an LLM. Every applicable restriction produces a Finding; the final decision is:

    any DENY              -> DENY
    else any REQUIRE_APPROVAL -> REQUIRE_APPROVAL
    else                  -> ALLOW

Missing (system/org/user/agent) or malformed policy documents fail closed (DENY), as does any
exception raised while evaluating. The merchant document is optional: without one the merchant imposes
no restrictions of its own. ``evaluate`` reads no clock; the decision depends only on its arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from pact_core.crypto import canonical_json, sha256_hex
from pact_core.policy.rules import SCHEMAS, AgentPolicy, MerchantPolicy, OrgPolicy, SystemPolicy, UserPolicy

#: Merchant Lab payment statuses (spec §8) from which an order may still be paid. APPROVED (in flight
#: at the provider) and CAPTURED (already paid) are not payable.
PAYABLE_ORDER_STATUSES = frozenset({"CREATED", "PENDING", "FAILED", "CANCELLED"})

#: Evaluation order of the policy scopes; also the order of ``policies_evaluated``.
SCOPES = ("system", "org", "user", "merchant", "agent")


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
    """The policy documents in force for one evaluation, each as ``(policy_id, version, rules)``.

    ``rules`` is normally the typed model for its scope. A raw ``dict`` (as stored in the database) is
    also accepted and validated during evaluation: a document that fails validation, or a model of the
    wrong scope, yields ``<SCOPE>_POLICY_INVALID`` and the decision fails closed.
    """

    system: tuple[str, int, SystemPolicy | dict[str, Any]] | None
    org: tuple[str, int, OrgPolicy | dict[str, Any]] | None
    user: tuple[str, int, UserPolicy | dict[str, Any]] | None
    merchant: tuple[str, int, MerchantPolicy | dict[str, Any]] | None
    agent: tuple[str, int, AgentPolicy | dict[str, Any]] | None

    def docs(self) -> list[tuple[str, tuple[str, int, Any] | None]]:
        return [(scope, getattr(self, scope)) for scope in SCOPES]

    def refs(self) -> list[PolicyRef]:
        return [PolicyRef(id=d[0], version=d[1]) for _, d in self.docs() if d]

    def version(self) -> str:
        """Content hash over every document's scope, id, version and (normalised) rules."""
        parts = []
        for scope, doc in self.docs():
            if not doc:
                continue
            model = _coerce_rules(scope, doc[2])
            rules = model.model_dump() if model is not None else doc[2]
            parts.append((scope, doc[0], doc[1], rules))
        return sha256_hex(canonical_json(parts))[:16]


def _coerce_rules(scope: str, rules: Any) -> BaseModel | None:
    """Return ``rules`` as the typed model for ``scope``, or None if it is not a valid document."""
    schema = SCHEMAS[scope]
    if type(rules) is schema:
        return rules
    if isinstance(rules, dict):
        try:
            return schema.model_validate(rules)
        except ValidationError:
            return None
    return None


def budget_scopes(snapshot: PolicySnapshot, agent_id: str, user_id: str) -> list[tuple[str, int]]:
    """The shared budgets a request draws from: (scope_key, limit). All must have headroom.

    Scopes whose document is missing or invalid are omitted; ``evaluate`` denies such requests anyway.
    """
    org = _coerce_rules("org", snapshot.org[2]) if snapshot.org else None
    user = _coerce_rules("user", snapshot.user[2]) if snapshot.user else None
    agent = _coerce_rules("agent", snapshot.agent[2]) if snapshot.agent else None
    scopes: list[tuple[str, int]] = []
    if org is not None:
        scopes.append(("org:global", org.daily_budget_minor))
    if user is not None:
        scopes.append((f"user:{user_id}", user.daily_limit_minor))
    if agent is not None:
        scopes.append((f"agent:{agent_id}", agent.daily_limit_minor))
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
    if isinstance(amt, bool) or not isinstance(amt, int):
        raise TypeError("amount_minor must be an integer number of minor units")

    def add(source: str, policy_id: str, rule: str, effect: Effect, code: str, message: str, limit=None, observed=None):
        findings.append(
            Finding(
                source=source, policy_id=policy_id, rule=rule, effect=effect, code=code,
                message=message, limit_minor=limit, observed_minor=observed,
            )
        )

    # ---- fail closed on missing or malformed policy documents -------------------------------
    # The merchant document is optional: a merchant without one imposes no merchant restrictions.
    docs: dict[str, tuple[str, int, Any] | None] = {}
    for name, doc in p.docs():
        docs[name] = None
        if doc is None:
            if name != "merchant":
                add(name, f"{name}:?", "presence", Effect.DENY, f"{name.upper()}_POLICY_MISSING",
                    f"No {name} policy is configured; failing closed.")
            continue
        rules = _coerce_rules(name, doc[2])
        if rules is None:
            add(name, doc[0], "schema", Effect.DENY, f"{name.upper()}_POLICY_INVALID",
                f"The {name} policy {doc[0]} v{doc[1]} is malformed; failing closed.")
            continue
        docs[name] = (doc[0], doc[1], rules)

    # ---- system integrity (always enforced, even without a usable system document) ----------
    sid = p.system[0] if p.system else "system:?"
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
        if o.order_id != f.order_id:
            mismatches.append("order")
        if o.merchant_id != f.merchant_id:
            mismatches.append("merchant")
        if o.amount_minor != amt:
            mismatches.append("amount")
        if o.currency.upper() != cur.upper():
            mismatches.append("currency")
        if mismatches:
            add("system", sid, "order_binding", Effect.DENY, "ORDER_MISMATCH",
                f"Requested {', '.join(mismatches)} does not match merchant order {o.order_id} "
                f"({_fmt(o.amount_minor, o.currency)} at {o.merchant_id}).",
                o.amount_minor, amt)
        if o.status.upper() not in PAYABLE_ORDER_STATUSES:
            add("system", sid, "order_state", Effect.DENY, "ORDER_NOT_PAYABLE",
                f"Order {o.order_id} is in status {o.status} and cannot be paid.")
    if f.order_has_active_authorization:
        add("system", sid, "duplicate_execution", Effect.DENY, "DUPLICATE_ORDER_AUTHORIZATION",
            f"Order {f.order_id} already has an active or completed authorization.")

    # ---- system (hard, configured) ------------------------------------------------------------
    if docs["system"]:
        sid, _, sp = docs["system"]
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

    # ---- organisation ----------------------------------------------------------------------
    if docs["org"]:
        oid, _, op = docs["org"]
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
    if docs["user"]:
        uid, _, up = docs["user"]
        if up.max_transaction_minor is not None and amt > up.max_transaction_minor:
            add("user", uid, "max_transaction", Effect.DENY, "USER_TRANSACTION_LIMIT",
                f"{_fmt(amt, cur)} exceeds the user's per-transaction limit.", up.max_transaction_minor, amt)
        if amt > up.approval_threshold_minor:
            add("user", uid, "approval_threshold", Effect.REQUIRE_APPROVAL, "USER_APPROVAL_THRESHOLD",
                f"{_fmt(amt, cur)} is above the user's approval threshold of {_fmt(up.approval_threshold_minor, cur)}.",
                up.approval_threshold_minor, amt)

    # ---- merchant --------------------------------------------------------------------------
    if docs["merchant"]:
        mid, _, mp = docs["merchant"]
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
    if docs["agent"]:
        aid, _, ap = docs["agent"]
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
        if used < 0:
            raise ValueError(f"budget usage for {scope_key} is negative")
        remaining = max(limit - used, 0)
        limits[f"budget_remaining[{scope_key}]"] = remaining
        if amt > remaining:
            add("budget", scope_key, "daily_budget", Effect.DENY, "BUDGET_EXCEEDED",
                f"{_fmt(amt, cur)} exceeds remaining {scope_key} budget of {_fmt(remaining, cur)} "
                f"(limit {_fmt(limit, cur)}, used {_fmt(used, cur)}).", limit, used + amt)

    # ---- effective per-transaction limit -----------------------------------------------------
    def rule(scope: str, attr: str) -> int | None:
        doc = docs[scope]
        return getattr(doc[2], attr) if doc else None

    caps = {
        "system.hard_max_transaction": rule("system", "hard_max_transaction_minor"),
        "org.max_transaction": rule("org", "max_transaction_minor"),
        "user.max_transaction": rule("user", "max_transaction_minor"),
        "merchant.max_transaction": rule("merchant", "max_transaction_minor"),
        "agent.max_transaction": rule("agent", "max_transaction_minor"),
    }
    limits.update(caps)
    limits["user.approval_threshold"] = rule("user", "approval_threshold_minor")
    limits["merchant.approval_required_above"] = rule("merchant", "approval_required_above_minor")
    limits["agent.approval_required_above"] = rule("agent", "approval_required_above_minor")
    tx_caps = [v for v in caps.values() if v is not None]
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
