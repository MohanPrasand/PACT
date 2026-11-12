"""Deterministic policy engine (issue #4, spec §5, §7.2, §14.1).

Pure-function tests: no database, network, LLM or clock. Every test builds an ``EvaluationInput`` and a
``PolicySnapshot`` through the factories below and asserts on the ``PolicyDecision``.

Defaults (USD, minor units): org daily budget 50000 with approved merchants [m_techmart]; user approval
threshold 5000 and daily limit 50000; merchant m_techmart max 25000; agent ag_shopping max 15000 and
daily 25000; system hard cap 100000. The default request is a 3000 purchase that is ALLOWed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pact_core.policy import engine
from pact_core.policy.engine import (
    PAYABLE_ORDER_STATUSES,
    Effect,
    EvaluationInput,
    Finding,
    OrderFacts,
    PolicyDecision,
    PolicySnapshot,
    budget_scopes,
    decision_dict,
    evaluate,
)
from pact_core.policy.rules import (
    SCHEMAS,
    AgentPolicy,
    MerchantPolicy,
    OrgPolicy,
    SystemPolicy,
    UserPolicy,
    validate_rules,
)

POLICY_DIR = Path(__file__).resolve().parents[1] / "specification" / "policies"

AGENT = "ag_shopping"
USER = "u_alice"
MERCHANT = "m_techmart"
ORDER = "ord_1"

DEFAULT_RULES: dict[str, dict[str, Any]] = {
    "system": {"currency": "USD", "hard_max_transaction_minor": 100_000, "allowed_actions": ["PURCHASE"]},
    "org": {
        "daily_budget_minor": 50_000,
        "approved_merchants": [MERCHANT],
        "duplicate_window_minutes": 10,
        "duplicate_effect": "REQUIRE_APPROVAL",
    },
    "user": {"daily_limit_minor": 50_000, "approval_threshold_minor": 5_000},
    "merchant": {"max_transaction_minor": 25_000},
    "agent": {"max_transaction_minor": 15_000, "daily_limit_minor": 25_000},
}
POLICY_IDS = {
    "system": "system:default",
    "org": "org:acme",
    "user": f"user:{USER}",
    "merchant": f"merchant:{MERCHANT}",
    "agent": f"agent:{AGENT}",
}
REQUIRED_SCOPES = ("system", "org", "user", "agent")

_DEFAULT = object()


# --------------------------------------------------------------------------------------------- factories


def make_order(
    amount: int = 3_000,
    *,
    order_id: str = ORDER,
    merchant_id: str = MERCHANT,
    currency: str = "USD",
    status: str = "CREATED",
    categories: tuple[str, ...] = ("peripherals",),
) -> OrderFacts:
    return OrderFacts(
        order_id=order_id,
        merchant_id=merchant_id,
        amount_minor=amount,
        currency=currency,
        status=status,
        categories=categories,
    )


def make_input(amount: int = 3_000, *, order: Any = _DEFAULT, order_kw: dict | None = None, **kw: Any) -> EvaluationInput:
    """A request whose authoritative order matches it, unless ``order``/``order_kw`` say otherwise."""
    fields: dict[str, Any] = {
        "agent_id": AGENT,
        "agent_active": True,
        "user_id": USER,
        "action_type": "PURCHASE",
        "merchant_id": MERCHANT,
        "order_id": ORDER,
        "amount_minor": amount,
        "currency": "USD",
        "merchant_exists": True,
    }
    fields.update(kw)
    if order is _DEFAULT:
        base = {
            "order_id": fields["order_id"],
            "merchant_id": fields["merchant_id"],
            "currency": fields["currency"],
        }
        base.update(order_kw or {})
        order = make_order(base.pop("amount", amount), **base)
    return EvaluationInput(order=order, **fields)


def make_snapshot(
    *,
    missing: tuple[str, ...] = (),
    versions: dict[str, int] | None = None,
    ids: dict[str, str] | None = None,
    raw: dict[str, Any] | None = None,
    **overrides: dict[str, Any],
) -> PolicySnapshot:
    """Default policies with per-scope rule ``overrides``; ``missing`` scopes are None; ``raw`` replaces
    a scope's rules object verbatim (e.g. an unvalidated dict or a wrong-typed model)."""
    versions = versions or {}
    ids = ids or {}
    raw = raw or {}
    slots: dict[str, Any] = {}
    for scope, schema in SCHEMAS.items():
        if scope in missing:
            slots[scope] = None
            continue
        if scope in raw:
            rules: Any = raw[scope]
        else:
            rules = schema.model_validate({**DEFAULT_RULES[scope], **overrides.get(scope, {})})
        slots[scope] = (ids.get(scope, POLICY_IDS[scope]), versions.get(scope, 1), rules)
    return PolicySnapshot(**slots)


def run(amount: int = 3_000, *, snap: PolicySnapshot | None = None, **kw: Any) -> PolicyDecision:
    return evaluate(make_input(amount, **kw), snap or make_snapshot())


def codes(d: PolicyDecision) -> set[str]:
    return {f.code for f in d.findings}


def finding(d: PolicyDecision, code: str) -> Finding:
    matches = [f for f in d.findings if f.code == code]
    assert matches, f"no finding {code} in {[f.code for f in d.findings]}"
    return matches[0]


def assert_deny(d: PolicyDecision, *expected: str) -> None:
    assert d.decision is Effect.DENY
    assert d.approval_required is False
    assert d.approval_reasons == []
    for code in expected:
        assert code in d.reason_codes, d.reason_codes


# ------------------------------------------------------------------------------------ spec §14.1 matrix


def test_default_request_is_allowed():
    d = run()
    assert d.decision is Effect.ALLOW
    assert d.reason_codes == ["WITHIN_POLICY"]
    assert d.findings == []
    assert d.approval_required is False
    assert d.approval_reasons == []


@pytest.mark.parametrize("amount", [1, 3_000, 4_999, 5_000])
def test_below_or_at_approval_threshold_allows(amount):
    assert run(amount).decision is Effect.ALLOW


@pytest.mark.parametrize("amount", [5_001, 10_000, 15_000])
def test_above_approval_threshold_requires_approval(amount):
    d = run(amount)
    assert d.decision is Effect.REQUIRE_APPROVAL
    assert d.reason_codes == ["USER_APPROVAL_THRESHOLD"]
    assert d.approval_required is True
    f = finding(d, "USER_APPROVAL_THRESHOLD")
    assert (f.source, f.policy_id, f.limit_minor, f.observed_minor) == ("user", POLICY_IDS["user"], 5_000, amount)


@pytest.mark.parametrize(
    ("scope", "field", "code"),
    [
        ("system", "hard_max_transaction_minor", "SYSTEM_TRANSACTION_LIMIT"),
        ("org", "max_transaction_minor", "ORG_TRANSACTION_LIMIT"),
        ("user", "max_transaction_minor", "USER_TRANSACTION_LIMIT"),
        ("merchant", "max_transaction_minor", "MERCHANT_TRANSACTION_LIMIT"),
        ("agent", "max_transaction_minor", "AGENT_TRANSACTION_LIMIT"),
    ],
)
def test_above_each_transaction_limit_denies(scope, field, code):
    snap = make_snapshot(**{scope: {field: 2_000}})
    d = run(3_000, snap=snap)
    assert_deny(d, code)
    assert d.reason_codes == [code]
    f = finding(d, code)
    assert (f.source, f.policy_id, f.effect) == (scope, POLICY_IDS[scope], Effect.DENY)
    assert (f.limit_minor, f.observed_minor) == (2_000, 3_000)
    # At the limit exactly is still allowed.
    assert run(2_000, snap=snap).decision is Effect.ALLOW


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (20_000, {"AGENT_TRANSACTION_LIMIT"}),
        (30_000, {"AGENT_TRANSACTION_LIMIT", "MERCHANT_TRANSACTION_LIMIT", "BUDGET_EXCEEDED"}),
        (120_000, {"AGENT_TRANSACTION_LIMIT", "MERCHANT_TRANSACTION_LIMIT", "SYSTEM_TRANSACTION_LIMIT", "BUDGET_EXCEEDED"}),
    ],
)
def test_default_caps_compose(amount, expected):
    d = run(amount)
    assert_deny(d, *expected)
    assert set(d.reason_codes) == expected


def test_agent_exceeds_own_limit_denies():
    d = run(15_001)
    assert_deny(d, "AGENT_TRANSACTION_LIMIT")
    assert d.reason_codes == ["AGENT_TRANSACTION_LIMIT"]


def test_unknown_merchant_denies():
    d = run(merchant_exists=False)
    assert_deny(d, "MERCHANT_UNKNOWN")
    assert finding(d, "MERCHANT_UNKNOWN").source == "system"


def test_not_approved_merchant_denies():
    snap = make_snapshot(org={"approved_merchants": ["m_other"]})
    d = run(snap=snap)
    assert_deny(d, "MERCHANT_NOT_APPROVED")
    assert d.reason_codes == ["MERCHANT_NOT_APPROVED"]
    assert finding(d, "MERCHANT_NOT_APPROVED").source == "org"


@pytest.mark.parametrize(
    ("scope_key", "used"),
    [
        ("org:global", 48_000),
        ("org:global", 50_000),
        ("org:global", 60_000),  # over-spent: remaining clamps at zero
        (f"user:{USER}", 47_001),
        (f"agent:{AGENT}", 22_001),
    ],
)
def test_budget_exhausted_denies(scope_key, used):
    d = run(3_000, budget_used_minor={scope_key: used})
    assert_deny(d, "BUDGET_EXCEEDED")
    assert d.reason_codes == ["BUDGET_EXCEEDED"]
    f = finding(d, "BUDGET_EXCEEDED")
    assert (f.source, f.policy_id) == ("budget", scope_key)
    assert f.observed_minor == used + 3_000
    assert d.applicable_limits[f"budget_remaining[{scope_key}]"] == max(f.limit_minor - used, 0)


@pytest.mark.parametrize(
    ("scope_key", "used"),
    [("org:global", 47_000), (f"user:{USER}", 47_000), (f"agent:{AGENT}", 22_000)],
)
def test_budget_exactly_sufficient_allows(scope_key, used):
    d = run(3_000, budget_used_minor={scope_key: used})
    assert d.decision is Effect.ALLOW
    assert d.applicable_limits[f"budget_remaining[{scope_key}]"] == 3_000


def test_budget_usage_of_unrelated_scopes_is_ignored():
    d = run(3_000, budget_used_minor={"agent:ag_other": 25_000, "user:u_bob": 50_000})
    assert d.decision is Effect.ALLOW


# --------------------------------------------------------------------------- composition and conflicts


def test_approval_plus_deny_is_deny():
    snap = make_snapshot(org={"approved_merchants": ["m_other"]})
    d = run(6_000, snap=snap)
    assert_deny(d, "MERCHANT_NOT_APPROVED")
    # The approval finding is still reported for explainability but does not drive the decision.
    assert "USER_APPROVAL_THRESHOLD" in codes(d)
    assert "USER_APPROVAL_THRESHOLD" not in d.reason_codes


def test_hard_system_deny_beats_every_approval():
    snap = make_snapshot(merchant={"approval_required_above_minor": 0}, agent={"approval_required_above_minor": 0})
    d = run(6_000, snap=snap, order_has_active_authorization=True)
    assert_deny(d, "DUPLICATE_ORDER_AUTHORIZATION")
    assert d.reason_codes == ["DUPLICATE_ORDER_AUTHORIZATION"]


def test_merchant_requires_approval_while_user_is_under_threshold():
    snap = make_snapshot(merchant={"approval_required_above_minor": 2_000})
    d = run(3_000, snap=snap)
    assert d.decision is Effect.REQUIRE_APPROVAL
    assert d.reason_codes == ["MERCHANT_APPROVAL_REQUIRED"]
    f = finding(d, "MERCHANT_APPROVAL_REQUIRED")
    assert (f.source, f.limit_minor, f.observed_minor) == ("merchant", 2_000, 3_000)


def test_merchant_allows_user_requires_approval():
    """Spec §14.1 conflict: merchant has no objection, user threshold demands approval."""
    snap = make_snapshot(merchant={"max_transaction_minor": 25_000, "approval_required_above_minor": 20_000})
    d = run(6_000, snap=snap)
    assert d.decision is Effect.REQUIRE_APPROVAL
    assert d.reason_codes == ["USER_APPROVAL_THRESHOLD"]


def test_multiple_approval_reasons_are_all_reported_once():
    snap = make_snapshot(
        merchant={"approval_required_above_minor": 1_000},
        agent={"approval_required_above_minor": 1_000},
    )
    d = run(6_000, snap=snap, similar_recent_payments=2)
    assert d.decision is Effect.REQUIRE_APPROVAL
    assert d.reason_codes == [
        "POSSIBLE_DUPLICATE_PAYMENT",
        "USER_APPROVAL_THRESHOLD",
        "MERCHANT_APPROVAL_REQUIRED",
        "AGENT_APPROVAL_REQUIRED",
    ]
    assert len(d.approval_reasons) == 4
    assert len(set(d.approval_reasons)) == 4


def test_repeated_reason_codes_are_deduplicated():
    used = {"org:global": 50_000, f"user:{USER}": 50_000, f"agent:{AGENT}": 25_000}
    d = run(3_000, budget_used_minor=used)
    assert_deny(d)
    assert d.reason_codes == ["BUDGET_EXCEEDED"]
    assert [f.policy_id for f in d.findings if f.code == "BUDGET_EXCEEDED"] == list(used)


# ------------------------------------------------------------------------------- merchant and agent rules


def test_merchant_category_restriction():
    snap = make_snapshot(merchant={"allowed_categories": ["peripherals", "displays"]})
    assert run(snap=snap, order_kw={"categories": ("peripherals", "displays")}).decision is Effect.ALLOW
    d = run(snap=snap, order_kw={"categories": ("peripherals", "groceries", "alcohol")})
    assert_deny(d, "MERCHANT_CATEGORY_NOT_ALLOWED")
    f = finding(d, "MERCHANT_CATEGORY_NOT_ALLOWED")
    assert f.source == "merchant"
    assert "alcohol, groceries" in f.message


def test_merchant_without_category_restriction_accepts_any_category():
    assert run(order_kw={"categories": ("anything",)}).decision is Effect.ALLOW


@pytest.mark.parametrize(("allowed", "expected"), [([AGENT], Effect.ALLOW), (["ag_research"], Effect.DENY), ([], Effect.DENY)])
def test_merchant_allowed_agents(allowed, expected):
    d = run(snap=make_snapshot(merchant={"allowed_agents": allowed}))
    assert d.decision is expected
    if expected is Effect.DENY:
        assert d.reason_codes == ["MERCHANT_AGENT_NOT_ALLOWED"]


def test_agent_policy_disabled_denies():
    d = run(snap=make_snapshot(agent={"enabled": False}))
    assert_deny(d, "AGENT_POLICY_DISABLED")
    assert finding(d, "AGENT_POLICY_DISABLED").source == "agent"


@pytest.mark.parametrize(("allowed", "expected"), [([MERCHANT], Effect.ALLOW), (["m_datahub"], Effect.DENY), ([], Effect.DENY)])
def test_agent_allowed_merchants(allowed, expected):
    d = run(snap=make_snapshot(agent={"allowed_merchants": allowed}))
    assert d.decision is expected
    if expected is Effect.DENY:
        assert d.reason_codes == ["AGENT_MERCHANT_NOT_ALLOWED"]


@pytest.mark.parametrize(("amount", "expected"), [(1_000, Effect.ALLOW), (1_001, Effect.REQUIRE_APPROVAL)])
def test_agent_approval_required_above(amount, expected):
    d = run(amount, snap=make_snapshot(agent={"approval_required_above_minor": 1_000}))
    assert d.decision is expected
    if expected is Effect.REQUIRE_APPROVAL:
        assert d.reason_codes == ["AGENT_APPROVAL_REQUIRED"]
        assert finding(d, "AGENT_APPROVAL_REQUIRED").source == "agent"


def test_agent_with_zero_limit_cannot_spend():
    d = run(1, snap=make_snapshot(agent={"max_transaction_minor": 0}))
    assert_deny(d, "AGENT_TRANSACTION_LIMIT")


# ------------------------------------------------------------------------------------- system (hard)


@pytest.mark.parametrize("currency", ["EUR", "INR"])
def test_currency_not_supported(currency):
    d = run(currency=currency)  # order is in the same currency, so this is not an order mismatch
    assert_deny(d, "CURRENCY_NOT_SUPPORTED")
    assert d.reason_codes == ["CURRENCY_NOT_SUPPORTED"]


def test_currency_comparison_is_case_insensitive():
    assert run(currency="usd").decision is Effect.ALLOW


@pytest.mark.parametrize("amount", [0, -1, -3_000])
def test_non_positive_amount_denies(amount):
    d = run(amount)
    assert_deny(d, "INVALID_AMOUNT")
    assert finding(d, "INVALID_AMOUNT").observed_minor == amount


@pytest.mark.parametrize("amount", [30.0, 30.5, True, "3000"])
def test_non_integer_amount_fails_closed(amount):
    d = run(amount)
    assert_deny(d, "POLICY_EVALUATION_ERROR")


@pytest.mark.parametrize("action", ["REFUND", "TRANSFER", "purchase", ""])
def test_action_not_allowed(action):
    d = run(action_type=action)
    assert_deny(d, "ACTION_NOT_ALLOWED")
    assert d.reason_codes == ["ACTION_NOT_ALLOWED"]


def test_agent_inactive_denies():
    d = run(agent_active=False)
    assert_deny(d, "AGENT_INACTIVE")
    assert d.reason_codes == ["AGENT_INACTIVE"]


def test_order_not_found_denies():
    d = run(order=None)
    assert_deny(d, "ORDER_NOT_FOUND")
    assert d.reason_codes == ["ORDER_NOT_FOUND"]


@pytest.mark.parametrize(
    ("order_kw", "field"),
    [
        ({"merchant_id": "m_datahub"}, "merchant"),
        ({"amount": 2_999}, "amount"),
        ({"amount": 30_000}, "amount"),
        ({"currency": "EUR"}, "currency"),
        ({"order_id": "ord_other"}, "order"),
    ],
)
def test_order_mismatch_for_each_bound_field(order_kw, field):
    """The agent's claim is checked against the authoritative merchant order, never trusted."""
    d = run(3_000, order_kw=order_kw)
    assert_deny(d, "ORDER_MISMATCH")
    assert d.reason_codes == ["ORDER_MISMATCH"]
    f = finding(d, "ORDER_MISMATCH")
    assert f.source == "system"
    assert f"Requested {field} does not match" in f.message


def test_order_mismatch_lists_every_mismatched_field():
    d = run(3_000, order_kw={"merchant_id": "m_datahub", "amount": 1, "currency": "EUR"})
    assert "merchant, amount, currency" in finding(d, "ORDER_MISMATCH").message


@pytest.mark.parametrize("status", ["APPROVED", "CAPTURED", "REFUNDED", "UNKNOWN", ""])
def test_order_status_not_payable(status):
    d = run(order_kw={"status": status})
    assert_deny(d, "ORDER_NOT_PAYABLE")
    assert d.reason_codes == ["ORDER_NOT_PAYABLE"]


@pytest.mark.parametrize("status", sorted(PAYABLE_ORDER_STATUSES) + ["created", "Pending"])
def test_order_status_payable(status):
    assert run(order_kw={"status": status}).decision is Effect.ALLOW


def test_duplicate_order_authorization_denies():
    d = run(order_has_active_authorization=True)
    assert_deny(d, "DUPLICATE_ORDER_AUTHORIZATION")
    assert d.reason_codes == ["DUPLICATE_ORDER_AUTHORIZATION"]


# ---------------------------------------------------------------------------------- duplicate window


@pytest.mark.parametrize(("effect", "expected"), [("DENY", Effect.DENY), ("REQUIRE_APPROVAL", Effect.REQUIRE_APPROVAL)])
def test_duplicate_window_effect(effect, expected):
    snap = make_snapshot(org={"duplicate_effect": effect})
    d = run(snap=snap, similar_recent_payments=1)
    assert d.decision is expected
    assert d.reason_codes == ["POSSIBLE_DUPLICATE_PAYMENT"]
    f = finding(d, "POSSIBLE_DUPLICATE_PAYMENT")
    assert (f.source, f.effect) == ("org", expected)
    assert d.approval_required is (expected is Effect.REQUIRE_APPROVAL)


@pytest.mark.parametrize("effect", ["DENY", "REQUIRE_APPROVAL"])
def test_duplicate_window_zero_disables_check(effect):
    snap = make_snapshot(org={"duplicate_window_minutes": 0, "duplicate_effect": effect})
    assert run(snap=snap, similar_recent_payments=5).decision is Effect.ALLOW


def test_no_similar_payments_no_duplicate_finding():
    snap = make_snapshot(org={"duplicate_effect": "DENY"})
    assert run(snap=snap, similar_recent_payments=0).decision is Effect.ALLOW


# --------------------------------------------------------------------------------------- fail closed


@pytest.mark.parametrize("scope", REQUIRED_SCOPES)
def test_missing_required_policy_fails_closed(scope):
    d = run(snap=make_snapshot(missing=(scope,)))
    code = f"{scope.upper()}_POLICY_MISSING"
    assert_deny(d, code)
    f = finding(d, code)
    assert (f.source, f.policy_id) == (scope, f"{scope}:?")
    assert POLICY_IDS[scope] not in {ref.id for ref in d.policies_evaluated}


def test_all_policies_missing_reports_each():
    d = run(snap=make_snapshot(missing=tuple(SCHEMAS)))
    assert_deny(d)
    assert d.reason_codes == [f"{s.upper()}_POLICY_MISSING" for s in REQUIRED_SCOPES]
    assert d.policies_evaluated == []


def test_missing_system_policy_still_enforces_integrity_checks():
    d = run(snap=make_snapshot(missing=("system",)), agent_active=False, order_has_active_authorization=True)
    assert_deny(d, "SYSTEM_POLICY_MISSING", "AGENT_INACTIVE", "DUPLICATE_ORDER_AUTHORIZATION")


def test_missing_merchant_policy_means_no_merchant_restrictions():
    """The merchant document is optional by design; its absence is not a deny."""
    snap = make_snapshot(missing=("merchant",))
    d = run(snap=snap, order_kw={"categories": ("anything",)})
    assert d.decision is Effect.ALLOW
    assert not any(f.source == "merchant" for f in d.findings)
    assert POLICY_IDS["merchant"] not in {ref.id for ref in d.policies_evaluated}
    assert d.applicable_limits["merchant.max_transaction"] is None
    assert d.applicable_limits["effective_max_transaction"] == 15_000
    # The remaining scopes still apply.
    assert run(6_000, snap=snap).decision is Effect.REQUIRE_APPROVAL
    assert run(20_000, snap=snap).reason_codes == ["AGENT_TRANSACTION_LIMIT"]


MALFORMED: list[tuple[str, dict[str, Any]]] = [
    ("system", {"currency": "USD", "hard_max_transaction_minor": 100_000, "max_amount": 5}),  # unknown field
    ("system", {"hard_max_transaction_minor": 0}),
    ("system", {"currency": "usd"}),
    ("org", {"daily_budget_minor": -1}),
    ("org", {"approved_merchants": [MERCHANT]}),  # required field missing
    ("org", {"daily_budget_minor": 50_000, "duplicate_effect": "ALLOW"}),
    ("user", {"daily_limit_minor": 50_000, "approval_threshold_minor": "5000"}),  # string, not int
    ("user", {"daily_limit_minor": 50_000, "approval_treshold_minor": 5_000}),  # typo
    ("merchant", {"max_transaction_minor": -100}),
    ("merchant", {"allowed_category": ["peripherals"]}),
    ("agent", {"max_transaction_minor": 15_000, "daily_limit_minor": 25_000, "enabled": "yes"}),
    ("agent", {"max_transaction_minor": 15_000}),
]


@pytest.mark.parametrize(("scope", "rules"), MALFORMED)
def test_malformed_policy_document_fails_closed(scope, rules):
    d = run(snap=make_snapshot(raw={scope: rules}))
    code = f"{scope.upper()}_POLICY_INVALID"
    assert_deny(d, code)
    f = finding(d, code)
    assert (f.source, f.policy_id) == (scope, POLICY_IDS[scope])
    assert d.policy_version != "error"


@pytest.mark.parametrize(
    ("scope", "wrong"),
    [
        ("org", UserPolicy(daily_limit_minor=1, approval_threshold_minor=1)),
        ("agent", "not a policy"),
        ("system", None.__class__),
        ("user", ["daily_limit_minor", 50_000]),
    ],
)
def test_wrong_typed_policy_document_fails_closed(scope, wrong):
    d = run(snap=make_snapshot(raw={scope: wrong}))
    assert_deny(d, f"{scope.upper()}_POLICY_INVALID")


@pytest.mark.parametrize("scope", list(SCHEMAS))
def test_valid_raw_dict_is_equivalent_to_typed_model(scope):
    typed = make_snapshot()
    raw = make_snapshot(raw={scope: dict(DEFAULT_RULES[scope])})
    for amount in (3_000, 6_000, 20_000):
        a = evaluate(make_input(amount), typed)
        b = evaluate(make_input(amount), raw)
        assert a.model_dump() == b.model_dump()


def test_injected_exception_fails_closed(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(engine, "_evaluate", boom)
    d = evaluate(make_input(), make_snapshot())
    assert_deny(d, "POLICY_EVALUATION_ERROR")
    assert d.reason_codes == ["POLICY_EVALUATION_ERROR"]
    assert d.policy_version == "error"
    assert d.policies_evaluated == []
    f = d.findings[0]
    assert (f.source, f.rule, f.effect) == ("system", "evaluation", Effect.DENY)
    assert "RuntimeError" in f.message
    assert "database exploded" not in f.message  # internals are not leaked into the decision


@pytest.mark.parametrize(
    ("facts", "snap"),
    [
        (None, None),
        ("garbage", make_snapshot()),
        (make_input(), "garbage"),
    ],
)
def test_garbage_arguments_fail_closed(facts, snap):
    assert_deny(evaluate(facts, snap), "POLICY_EVALUATION_ERROR")


def test_negative_budget_usage_fails_closed():
    d = run(budget_used_minor={"org:global": -10_000})
    assert_deny(d, "POLICY_EVALUATION_ERROR")


# --------------------------------------------------------------------------------------- determinism


DETERMINISM_CASES = [
    {},
    {"amount": 6_000},
    {"amount": 20_000},
    {"amount": 3_000, "similar_recent_payments": 1},
    {"amount": 3_000, "budget_used_minor": {"org:global": 49_000}},
    {"amount": 3_000, "order": None},
]


@pytest.mark.parametrize("case", DETERMINISM_CASES)
def test_same_input_same_decision(case):
    case = dict(case)
    amount = case.pop("amount", 3_000)
    first = evaluate(make_input(amount, **case), make_snapshot())
    second = evaluate(make_input(amount, **case), make_snapshot())
    assert first.model_dump() == second.model_dump()
    assert first.policy_version == second.policy_version
    assert json.dumps(decision_dict(first), sort_keys=True) == json.dumps(decision_dict(second), sort_keys=True)


def test_repeated_evaluation_of_one_object_is_stable():
    facts, snap = make_input(6_000), make_snapshot()
    results = {json.dumps(decision_dict(evaluate(facts, snap)), sort_keys=True) for _ in range(20)}
    assert len(results) == 1


def test_policy_version_ignores_request_facts():
    snap = make_snapshot()
    assert run(3_000, snap=snap).policy_version == run(20_000, snap=snap).policy_version


RULE_CHANGES = [
    ("system", {"hard_max_transaction_minor": 99_999}),
    ("system", {"allowed_actions": ["PURCHASE", "REFUND"]}),
    ("org", {"daily_budget_minor": 50_001}),
    ("org", {"approved_merchants": [MERCHANT, "m_datahub"]}),
    ("org", {"duplicate_effect": "DENY"}),
    ("user", {"approval_threshold_minor": 5_001}),
    ("user", {"max_transaction_minor": 40_000}),
    ("merchant", {"allowed_categories": ["peripherals"]}),
    ("merchant", {"approval_required_above_minor": 10_000}),
    ("agent", {"enabled": False}),
    ("agent", {"daily_limit_minor": 24_999}),
]


@pytest.mark.parametrize(("scope", "change"), RULE_CHANGES)
def test_policy_version_changes_when_rules_change(scope, change):
    base = make_snapshot().version()
    assert make_snapshot(**{scope: change}).version() != base


@pytest.mark.parametrize("scope", list(SCHEMAS))
def test_policy_version_changes_when_version_or_id_changes(scope):
    base = make_snapshot().version()
    assert make_snapshot(versions={scope: 2}).version() != base
    assert make_snapshot(ids={scope: f"{scope}:renamed"}).version() != base


@pytest.mark.parametrize("scope", list(SCHEMAS))
def test_policy_version_changes_when_a_document_is_removed(scope):
    assert make_snapshot(missing=(scope,)).version() != make_snapshot().version()


def test_policy_version_is_compact_hex_digest():
    v = make_snapshot().version()
    assert len(v) == 16
    int(v, 16)


def test_decision_has_no_time_dependent_fields():
    """``evaluate`` must not read the wall clock: nothing in the decision may depend on time."""
    assert set(PolicyDecision.model_fields) == {
        "decision",
        "reason_codes",
        "findings",
        "policies_evaluated",
        "applicable_limits",
        "approval_required",
        "approval_reasons",
        "policy_version",
    }
    assert set(Finding.model_fields) == {
        "source", "policy_id", "rule", "effect", "code", "message", "limit_minor", "observed_minor",
    }
    for name in ("datetime", "time", "date"):
        assert name not in vars(engine), f"engine imports {name}"


# -------------------------------------------------------------------------------------- output shape


def test_findings_carry_source_policy_limit_observed():
    d = run(20_000)
    f = finding(d, "AGENT_TRANSACTION_LIMIT")
    assert f.source == "agent"
    assert f.policy_id == POLICY_IDS["agent"]
    assert f.rule == "max_transaction"
    assert f.effect is Effect.DENY
    assert (f.limit_minor, f.observed_minor) == (15_000, 20_000)
    assert "200.00 USD" in f.message and "150.00 USD" in f.message


def test_policies_evaluated_lists_ids_and_versions_in_scope_order():
    d = run(snap=make_snapshot(versions={"org": 3, "agent": 7}))
    assert [(r.id, r.version) for r in d.policies_evaluated] == [
        (POLICY_IDS["system"], 1),
        (POLICY_IDS["org"], 3),
        (POLICY_IDS["user"], 1),
        (POLICY_IDS["merchant"], 1),
        (POLICY_IDS["agent"], 7),
    ]


def test_applicable_limits_default():
    limits = run().applicable_limits
    assert limits["system.hard_max_transaction"] == 100_000
    assert limits["org.max_transaction"] is None
    assert limits["user.max_transaction"] is None
    assert limits["merchant.max_transaction"] == 25_000
    assert limits["agent.max_transaction"] == 15_000
    assert limits["user.approval_threshold"] == 5_000
    assert limits["merchant.approval_required_above"] is None
    assert limits["agent.approval_required_above"] is None
    assert limits["effective_max_transaction"] == 15_000
    assert limits["budget_remaining[org:global]"] == 50_000
    assert limits[f"budget_remaining[user:{USER}]"] == 50_000
    assert limits[f"budget_remaining[agent:{AGENT}]"] == 25_000


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"org": {"max_transaction_minor": 12_000}}, 12_000),
        ({"user": {"max_transaction_minor": 8_000}}, 8_000),
        ({"merchant": {"max_transaction_minor": 9_000}}, 9_000),
        ({"system": {"hard_max_transaction_minor": 7_000}}, 7_000),
        ({"agent": {"max_transaction_minor": 30_000}}, 25_000),  # merchant cap becomes the binding one
        ({"org": {"max_transaction_minor": 11_000}, "user": {"max_transaction_minor": 10_000}}, 10_000),
    ],
)
def test_effective_max_transaction_is_min_of_caps(overrides, expected):
    limits = run(snap=make_snapshot(**overrides)).applicable_limits
    assert limits["effective_max_transaction"] == expected


def test_approval_reasons_only_on_require_approval():
    allow = run(3_000)
    approve = run(6_000)
    deny = run(20_000, snap=make_snapshot(agent={"approval_required_above_minor": 1_000}))
    assert allow.approval_reasons == [] and allow.approval_required is False
    assert approve.approval_required is True
    assert approve.approval_reasons == [finding(approve, "USER_APPROVAL_THRESHOLD").message]
    assert deny.decision is Effect.DENY
    assert deny.approval_reasons == [] and deny.approval_required is False


def test_decision_dict_is_json_serialisable():
    d = decision_dict(run(6_000))
    assert d["decision"] == "REQUIRE_APPROVAL"
    assert d["findings"][0]["effect"] == "REQUIRE_APPROVAL"
    assert d["policies_evaluated"][0] == {"id": POLICY_IDS["system"], "version": 1}
    assert json.loads(json.dumps(d)) == d


def test_budget_scopes():
    assert budget_scopes(make_snapshot(), AGENT, USER) == [
        ("org:global", 50_000),
        (f"user:{USER}", 50_000),
        (f"agent:{AGENT}", 25_000),
    ]
    assert budget_scopes(make_snapshot(missing=("org", "agent")), AGENT, USER) == [(f"user:{USER}", 50_000)]
    assert budget_scopes(make_snapshot(raw={"user": {"bad": 1}}), AGENT, USER) == [
        ("org:global", 50_000),
        (f"agent:{AGENT}", 25_000),
    ]
    assert budget_scopes(make_snapshot(raw={"org": {"daily_budget_minor": 1}}), AGENT, USER)[0] == ("org:global", 1)


# ---------------------------------------------------------------------------------- rules validation


MINIMAL_RULES = {
    "system": {},
    "org": {"daily_budget_minor": 0},
    "user": {"daily_limit_minor": 0, "approval_threshold_minor": 0},
    "merchant": {},
    "agent": {"max_transaction_minor": 0, "daily_limit_minor": 0},
}


@pytest.mark.parametrize("scope", list(SCHEMAS))
def test_validate_rules_accepts_minimal_documents_and_fills_defaults(scope):
    out = validate_rules(scope, MINIMAL_RULES[scope])
    assert out == SCHEMAS[scope].model_validate(MINIMAL_RULES[scope]).model_dump()
    assert set(out) == set(SCHEMAS[scope].model_fields)


def test_validate_rules_defaults():
    assert validate_rules("system", {}) == {
        "currency": "USD",
        "hard_max_transaction_minor": 100_000,
        "allowed_actions": ["PURCHASE"],
    }
    org = validate_rules("org", {"daily_budget_minor": 1})
    assert (org["duplicate_window_minutes"], org["duplicate_effect"], org["max_transaction_minor"]) == (
        10,
        "REQUIRE_APPROVAL",
        None,
    )
    assert validate_rules("agent", MINIMAL_RULES["agent"])["enabled"] is True


@pytest.mark.parametrize("scope", list(SCHEMAS))
def test_unknown_fields_rejected(scope):
    with pytest.raises(ValidationError, match="extra"):
        validate_rules(scope, {**MINIMAL_RULES[scope], "unexpected_rule": 1})


NEGATIVE_FIELDS = [
    ("system", "hard_max_transaction_minor"),
    ("org", "daily_budget_minor"),
    ("org", "max_transaction_minor"),
    ("org", "duplicate_window_minutes"),
    ("user", "daily_limit_minor"),
    ("user", "approval_threshold_minor"),
    ("user", "max_transaction_minor"),
    ("merchant", "max_transaction_minor"),
    ("merchant", "approval_required_above_minor"),
    ("agent", "max_transaction_minor"),
    ("agent", "daily_limit_minor"),
    ("agent", "approval_required_above_minor"),
]


@pytest.mark.parametrize(("scope", "field"), NEGATIVE_FIELDS)
def test_negative_limits_rejected(scope, field):
    with pytest.raises(ValidationError):
        validate_rules(scope, {**MINIMAL_RULES[scope], field: -1})


@pytest.mark.parametrize(
    ("scope", "field"),
    [("system", "hard_max_transaction_minor"), ("org", "max_transaction_minor"), ("user", "max_transaction_minor"),
     ("merchant", "max_transaction_minor")],
)
def test_optional_transaction_caps_must_be_positive(scope, field):
    with pytest.raises(ValidationError):
        validate_rules(scope, {**MINIMAL_RULES[scope], field: 0})


@pytest.mark.parametrize(
    ("scope", "field", "value"),
    [
        ("user", "approval_threshold_minor", "5000"),
        ("user", "approval_threshold_minor", 50.5),
        ("org", "daily_budget_minor", True),
        ("agent", "enabled", "false"),
        ("agent", "enabled", 0),
        ("system", "currency", "usd"),
        ("system", "currency", "DOLLARS"),
        ("org", "duplicate_effect", "ALLOW"),
        ("merchant", "allowed_agents", "ag_shopping"),
    ],
)
def test_wrong_types_rejected_without_coercion(scope, field, value):
    with pytest.raises(ValidationError):
        validate_rules(scope, {**MINIMAL_RULES[scope], field: value})


def test_validate_rules_unknown_scope_raises():
    with pytest.raises(ValueError, match="Unknown policy scope"):
        validate_rules("global", {})


def test_schema_registry_covers_every_scope():
    assert SCHEMAS == {
        "system": SystemPolicy,
        "org": OrgPolicy,
        "user": UserPolicy,
        "merchant": MerchantPolicy,
        "agent": AgentPolicy,
    }


# --------------------------------------------------------------------------------- example policies


EXPECTED_EXAMPLES = {
    "system.json",
    "org.json",
    "user.json",
    "merchant.m_techmart.json",
    "merchant.m_datahub.json",
    "agent.ag_shopping.json",
    "agent.ag_research.json",
    "agent.ag_procurement.json",
}


def _load_example(name: str) -> dict[str, Any]:
    return json.loads((POLICY_DIR / name).read_text())


def test_example_policy_files_present():
    assert EXPECTED_EXAMPLES <= {p.name for p in POLICY_DIR.glob("*.json")}


@pytest.mark.parametrize("path", sorted(POLICY_DIR.glob("*.json")), ids=lambda p: p.name)
def test_example_policy_validates(path):
    scope = path.name.split(".")[0]
    rules = json.loads(path.read_text())
    assert validate_rules(scope, rules) == {**validate_rules(scope, rules), **rules}


def _example_snapshot(agent: str, merchant: str | None) -> PolicySnapshot:
    def doc(scope: str, name: str, subject: str) -> tuple[str, int, Any]:
        return (f"{scope}:{subject}", 1, SCHEMAS[scope].model_validate(_load_example(name)))

    return PolicySnapshot(
        system=doc("system", "system.json", "default"),
        org=doc("org", "org.json", "acme"),
        user=doc("user", "user.json", USER),
        merchant=doc("merchant", f"merchant.{merchant}.json", merchant) if merchant else None,
        agent=doc("agent", f"agent.{agent}.json", agent),
    )


@pytest.mark.parametrize(
    ("agent", "merchant", "amount", "categories", "decision", "reasons"),
    [
        ("ag_shopping", "m_techmart", 3_000, ("peripherals",), Effect.ALLOW, ["WITHIN_POLICY"]),
        ("ag_shopping", "m_techmart", 12_000, ("displays",), Effect.REQUIRE_APPROVAL, ["USER_APPROVAL_THRESHOLD"]),
        ("ag_shopping", "m_techmart", 3_000, ("groceries",), Effect.DENY, ["MERCHANT_CATEGORY_NOT_ALLOWED"]),
        ("ag_shopping", "m_datahub", 3_000, ("datasets",), Effect.DENY, ["MERCHANT_AGENT_NOT_ALLOWED"]),
        ("ag_research", "m_datahub", 4_000, ("datasets",), Effect.ALLOW, ["WITHIN_POLICY"]),
        ("ag_research", "m_datahub", 12_000, ("datasets",), Effect.DENY,
         ["MERCHANT_TRANSACTION_LIMIT", "AGENT_TRANSACTION_LIMIT"]),
        ("ag_research", "m_techmart", 3_000, ("audio",), Effect.DENY, ["AGENT_MERCHANT_NOT_ALLOWED"]),
        ("ag_procurement", "m_techmart", 20_000, ("displays",), Effect.REQUIRE_APPROVAL, ["USER_APPROVAL_THRESHOLD"]),
        ("ag_procurement", "m_techmart", 26_000, ("displays",), Effect.DENY,
         ["MERCHANT_TRANSACTION_LIMIT", "AGENT_TRANSACTION_LIMIT"]),
    ],
)
def test_example_policies_end_to_end(agent, merchant, amount, categories, decision, reasons):
    facts = make_input(amount, agent_id=agent, merchant_id=merchant, order_kw={"categories": categories})
    d = evaluate(facts, _example_snapshot(agent, merchant))
    assert d.decision is decision
    assert d.reason_codes == reasons


def test_example_org_duplicate_window_requires_approval():
    facts = make_input(3_000, similar_recent_payments=1)
    d = evaluate(facts, _example_snapshot("ag_shopping", "m_techmart"))
    assert d.decision is Effect.REQUIRE_APPROVAL
    assert d.reason_codes == ["POSSIBLE_DUPLICATE_PAYMENT"]
