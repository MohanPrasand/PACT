"""#8 Authorization service: request, evaluate, approve, deny, revoke, expire, grant delivery."""

from __future__ import annotations

import threading

import pytest

from pact_core.errors import Forbidden, NotFound, PactError
from tests.pact_env import build_env


@pytest.fixture
def env(db, clock):
    return build_env(db, clock)


def events(env, rid):
    return [e["event_type"] for e in reversed(env.core.audit_events(request_id=rid))]


def scope(env, key, agent=None):
    return next(s for s in env.core.budget_status(agent)["scopes"] if s["scope"] == key)


# ---------------------------------------------------------------- identity

def test_authentication(env):
    assert env.agent("ag_shopping").user_id == "u_demo"
    with pytest.raises(Forbidden) as e:
        env.core.authenticate_agent("pact_ak_wrong")
    assert e.value.code == "AGENT_AUTH_INVALID"
    with pytest.raises(Forbidden):
        env.core.authenticate_agent("")


# ---------------------------------------------------------------- decisions

def test_below_threshold_is_allowed_and_granted_immediately(env):
    agent = env.agent()
    order = env.order(env.product("mouse")["product_id"])  # $29 < $50 threshold
    v = env.core.request_authorization(agent, env.auth_input(order))
    assert v["state"] == "GRANTED"
    assert v["decision"]["decision"] == "ALLOW"
    assert v["grant_token"].startswith("pact_gr_")
    assert scope(env, "org:global")["reserved_minor"] == order["total_minor"]
    assert events(env, v["request_id"]) == ["REQUEST_CREATED", "BUDGET_RESERVED", "POLICY_EVALUATED", "APPROVED",
                                             "GRANT_ISSUED", "GRANT_DELIVERED"]


def test_above_threshold_requires_approval_and_reserves(env):
    agent = env.agent()
    order = env.order(env.product("Atlas")["product_id"])  # $79
    v = env.core.request_authorization(agent, env.auth_input(order))
    assert v["state"] == "PENDING_APPROVAL"
    assert v["decision"]["reason_codes"] == ["USER_APPROVAL_THRESHOLD"]
    assert "grant_token" not in v
    assert scope(env, "org:global")["reserved_minor"] == 7900  # pending reserves its full amount


def test_unapproved_merchant_is_denied_without_reservation(env):
    agent = env.agent()
    order = env.order(env.product("Unbranded")["product_id"])
    v = env.core.request_authorization(agent, env.auth_input(order))
    assert v["state"] == "DENIED"
    assert "MERCHANT_NOT_APPROVED" in v["decision"]["reason_codes"]
    assert scope(env, "org:global")["reserved_minor"] == 0
    assert "BUDGET_RESERVED" not in events(env, v["request_id"])


def test_claimed_amount_must_match_merchant_order(env):
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order, amount_minor=100))
    assert v["state"] == "DENIED" and "ORDER_MISMATCH" in v["decision"]["reason_codes"]


def test_agent_transaction_limit(env):
    order = env.order(env.product("Monitor")["product_id"])  # $189 > shopping agent's $150
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    assert v["state"] == "DENIED" and "AGENT_TRANSACTION_LIMIT" in v["decision"]["reason_codes"]


def test_duplicate_authorization_for_same_order_denied(env):
    agent = env.agent()
    order = env.order(env.product("mouse")["product_id"])
    first = env.core.request_authorization(agent, env.auth_input(order))
    second = env.core.request_authorization(agent, env.auth_input(order))
    assert first["state"] == "GRANTED"
    assert second["state"] == "DENIED" and "DUPLICATE_ORDER_AUTHORIZATION" in second["decision"]["reason_codes"]


def test_similar_payment_in_window_requires_approval(env):
    agent = env.agent()
    mouse = env.product("mouse")["product_id"]
    env.core.request_authorization(agent, env.auth_input(env.order(mouse)))
    v = env.core.request_authorization(agent, env.auth_input(env.order(mouse)))
    assert v["state"] == "PENDING_APPROVAL" and "POSSIBLE_DUPLICATE_PAYMENT" in v["decision"]["reason_codes"]


def test_invalid_input_rejected(env):
    order = env.order(env.product("mouse")["product_id"])
    with pytest.raises(PactError) as e:
        env.core.request_authorization(env.agent(), env.auth_input(order, amount_minor="29.00"))
    assert e.value.code == "INVALID_REQUEST"
    with pytest.raises(PactError):
        env.core.request_authorization(env.agent(), {**env.auth_input(order), "user_id": "someone_else"})


def test_evaluate_policy_is_a_dry_run(env):
    order = env.order(env.product("Atlas")["product_id"])
    r = env.core.evaluate_policy(env.agent(), env.auth_input(order))
    assert r["dry_run"] and r["decision"]["decision"] == "REQUIRE_APPROVAL"
    assert env.core.list_requests() == []
    assert scope(env, "org:global")["reserved_minor"] == 0
    assert env.core.verify_audit()["events_checked"] == 0


# ---------------------------------------------------------------- shared budget

def test_shared_budget_across_agents(env):
    """Spec §2: each agent within its own cap, but together they exceed the org budget."""
    env.set_policy("org:default", daily_budget_minor=20000)
    env.set_policy("user:u_demo", approval_threshold_minor=100000)  # isolate the budget behaviour
    shop = env.core.request_authorization(env.agent("ag_shopping"),
                                          env.auth_input(env.order(env.product("Headphones")["product_id"])))  # $119
    research = env.core.request_authorization(env.agent("ag_research"), env.auth_input(
        env.order(env.product("Retail Price Index")["product_id"], buyer="ag_research")))  # $45
    procurement = env.core.request_authorization(env.agent("ag_procurement"), env.auth_input(
        env.order(env.product("Atlas")["product_id"], buyer="ag_procurement")))  # $79 -> 243 > 200
    assert shop["state"] == "GRANTED" and research["state"] == "GRANTED"
    assert procurement["state"] == "DENIED"
    assert "BUDGET_EXCEEDED" in procurement["decision"]["reason_codes"]
    finding = next(f for f in procurement["decision"]["findings"] if f["code"] == "BUDGET_EXCEEDED")
    assert finding["policy_id"] == "org:global"


def test_concurrent_requests_never_overspend(env):
    env.set_policy("org:default", daily_budget_minor=2900 * 3, duplicate_window_minutes=0)
    env.set_policy("user:u_demo", approval_threshold_minor=100000)
    mouse = env.product("mouse")["product_id"]
    orders = [env.order(mouse) for _ in range(8)]
    agent = env.agent()
    results, barrier = [], threading.Barrier(len(orders))

    def run(o):
        barrier.wait()
        results.append(env.core.request_authorization(agent, env.auth_input(o))["state"])

    threads = [threading.Thread(target=run, args=(o,)) for o in orders]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert results.count("GRANTED") == 3 and results.count("DENIED") == 5
    assert scope(env, "org:global")["available_minor"] == 0
    with env.core.db.transaction() as s:
        assert env.core.ctx.budget.check_invariants(s) == []


# ---------------------------------------------------------------- approval & grant delivery

def test_approval_then_single_delivery_of_grant(env):
    agent = env.agent()
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(agent, env.auth_input(order))
    a = env.core.approve("alice", v["request_id"], v["request_hash"], note="ok")
    assert a["state"] == "APPROVED" and a["decided_by"] == "admin:alice" and "grant_token" not in a
    first = env.core.get_status(agent, v["request_id"])
    second = env.core.get_status(agent, v["request_id"])
    assert first["state"] == "GRANTED" and first["grant_token"].startswith("pact_gr_")
    assert second["state"] == "GRANTED" and "grant_token" not in second
    assert second["grant"]["consumed"] is False


def test_approval_requires_displayed_hash(env):
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    with pytest.raises(PactError) as e:
        env.core.approve("alice", v["request_id"], "0" * 64)
    assert e.value.code == "APPROVAL_HASH_MISMATCH"
    assert env.core.get_status(env.agent(), v["request_id"])["state"] == "PENDING_APPROVAL"


def test_order_modified_before_approval_is_denied(env):
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    env.lab.update_order_items(order["order_id"], [{"product_id": env.product("Atlas")["product_id"], "quantity": 2}])
    with pytest.raises(PactError) as e:
        env.core.approve("alice", v["request_id"], v["request_hash"])
    assert e.value.code == "ORDER_MODIFIED_SINCE_REQUEST"
    assert env.core.get_status(env.agent(), v["request_id"])["state"] == "DENIED"
    assert scope(env, "org:global")["reserved_minor"] == 0


def test_cannot_approve_a_denied_request(env):
    order = env.order(env.product("Unbranded")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    with pytest.raises(PactError) as e:
        env.core.approve("alice", v["request_id"], v["request_hash"])
    assert e.value.code == "NOT_PENDING_APPROVAL"


def test_deny_releases_reservation(env):
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    d = env.core.deny("alice", v["request_id"], "not needed")
    assert d["state"] == "DENIED" and d["decision_note"] == "not needed"
    assert scope(env, "org:global")["reserved_minor"] == 0


def test_requests_are_private_to_their_agent(env):
    order = env.order(env.product("mouse")["product_id"])
    v = env.core.request_authorization(env.agent("ag_shopping"), env.auth_input(order))
    with pytest.raises(NotFound):
        env.core.get_status(env.agent("ag_research"), v["request_id"])
    with pytest.raises(NotFound):
        env.core.revoke(env.agent("ag_research"), v["request_id"])
    assert env.core.agent_audit(env.agent("ag_research")) == []
    assert env.core.agent_audit(env.agent("ag_shopping"), v["request_id"])


# ---------------------------------------------------------------- revoke & expiry

@pytest.mark.parametrize("stage", ["pending", "approved", "granted"])
def test_revoke_unused_authorization(env, stage):
    agent = env.agent()
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(agent, env.auth_input(order))
    if stage in ("approved", "granted"):
        env.core.approve("alice", v["request_id"], v["request_hash"])
    if stage == "granted":
        env.core.get_status(agent, v["request_id"])
    r = env.core.revoke(agent, v["request_id"])
    assert r["state"] == "REVOKED"
    assert scope(env, "org:global")["reserved_minor"] == 0
    if stage == "granted":
        assert r["grant"]["revoked"] is True
    with pytest.raises(PactError) as e:
        env.core.revoke(agent, v["request_id"])
    assert e.value.code == "NOT_REVOCABLE"


def test_pending_approval_expires(env):
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    env.clock.advance(seconds=1801)
    assert env.core.expire_stale() == [v["request_id"]]
    s = env.core.get_status(env.agent(), v["request_id"])
    assert s["state"] == "EXPIRED" and s["execution"]["failure_reason"] == "APPROVAL_WINDOW_ELAPSED"
    assert scope(env, "org:global")["reserved_minor"] == 0


def test_approval_after_window_is_refused(env):
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    env.clock.advance(seconds=1801)
    with pytest.raises(PactError) as e:
        env.core.approve("alice", v["request_id"], v["request_hash"])
    assert e.value.code == "APPROVAL_EXPIRED"
    assert env.core.get_status(env.agent(), v["request_id"])["state"] == "EXPIRED"


def test_unclaimed_approval_expires(env):
    order = env.order(env.product("Atlas")["product_id"])
    v = env.core.request_authorization(env.agent(), env.auth_input(order))
    env.core.approve("alice", v["request_id"], v["request_hash"])
    env.clock.advance(seconds=1801)
    s = env.core.get_status(env.agent(), v["request_id"])
    assert s["state"] == "EXPIRED" and "grant_token" not in s


def test_unused_grant_expires(env):
    order, v, _ = env.granted("mouse")
    env.clock.advance(seconds=601)
    assert env.core.expire_stale() == [v["request_id"]]
    assert env.core.get_status(env.agent(), v["request_id"])["state"] == "EXPIRED"
    assert scope(env, "org:global")["reserved_minor"] == 0


def test_policy_update_validated_and_versioned(env):
    current = next(p for p in env.core.list_policies() if p["id"] == "user:u_demo")
    with pytest.raises(PactError) as e:
        env.core.update_policy("alice", "user:u_demo", {**current["rules"], "bogus": 1}, current["version"])
    assert e.value.code == "POLICY_INVALID"
    with pytest.raises(PactError) as e:
        env.core.update_policy("alice", "user:u_demo", current["rules"], current["version"] + 5)
    assert e.value.code == "POLICY_VERSION_CONFLICT"
    updated = env.core.update_policy("alice", "user:u_demo", {**current["rules"], "approval_threshold_minor": 10000},
                                     current["version"])
    assert updated["version"] == current["version"] + 1
    assert env.core.audit_events(event_type="POLICY_UPDATED")[0]["data"]["after"]["approval_threshold_minor"] == 10000
    # The new threshold applies immediately: a $79 keyboard is now auto-approved.
    v = env.core.request_authorization(env.agent(), env.auth_input(env.order(env.product("Atlas")["product_id"])))
    assert v["state"] == "GRANTED"


def test_missing_policy_fails_closed(env):
    with env.core.db.transaction() as s:
        from pact_core.models import Policy
        s.delete(s.get(Policy, "agent:ag_shopping"))
    v = env.core.request_authorization(env.agent(), env.auth_input(env.order(env.product("mouse")["product_id"])))
    assert v["state"] == "DENIED" and "AGENT_POLICY_MISSING" in v["decision"]["reason_codes"]


def test_audit_chain_valid_after_mixed_activity(env):
    env.granted("mouse")
    env.granted("Atlas")
    env.core.request_authorization(env.agent(), env.auth_input(env.order(env.product("Unbranded")["product_id"])))
    assert env.core.verify_audit()["valid"] is True
