"""#10 Execution gateway: validation chain, idempotency, provider lifecycle, UNKNOWN and reconciliation.
Uses the simulation adapter (spec §14.4); PayPal-specific parsing is covered in test_paypal_adapter.py."""

from __future__ import annotations

import threading

import pytest

from pact_core.errors import PactError
from tests.pact_env import build_env


@pytest.fixture
def env(db, clock):
    return build_env(db, clock)


def events(env, rid):
    return [e["event_type"] for e in reversed(env.core.audit_events(request_id=rid))]


def org(env):
    return next(s for s in env.core.budget_status()["scopes"] if s["scope"] == "org:global")


def start(env, product="Atlas", **kw):
    order, view, token = env.granted(product)
    v = env.core.execute(env.agent(), env.exec_input(view, order, token, **kw))
    return order, view, token, v


def test_happy_path_capture_settles_and_marks_order_paid(env):
    order, view, token, v = start(env)
    assert v["state"] == "EXECUTING"
    assert v["execution"]["provider_label"] == "Simulation (not PayPal)"
    assert v["execution"]["approval_url"].endswith(v["execution"]["provider_order_id"])
    assert env.lab.get_order(order["order_id"])["payment_status"] == "PENDING"

    env.sim.buyer_approve(v["execution"]["provider_order_id"])
    done = env.core.handle_provider_return(v["execution"]["provider_order_id"])
    assert done["state"] == "EXECUTED" and done["execution"]["capture_id"].startswith("SIMCAP-")
    paid = env.lab.get_order(order["order_id"])
    assert paid["payment_status"] == "CAPTURED" and paid["provider_ref"] == done["execution"]["capture_id"]
    assert org(env)["settled_minor"] == 7900 and org(env)["reserved_minor"] == 0
    assert env.core.verify_audit()["valid"]
    with env.core.db.transaction() as s:
        assert env.core.ctx.budget.check_invariants(s) == []


def test_replayed_grant_is_rejected_and_audited(env):
    """Spec §14.5: the agent tries to reuse the grant after a completed payment."""
    order, view, token, v = start(env)
    env.sim.buyer_approve(v["execution"]["provider_order_id"])
    env.core.handle_provider_return(v["execution"]["provider_order_id"])
    with pytest.raises(PactError) as e:
        env.core.execute(env.agent(), env.exec_input(view, order, token))
    assert e.value.code == "GRANT_ALREADY_USED"
    assert events(env, view["request_id"])[-1] == "REPLAY_REJECTED"
    assert len(env.core.audit_events(event_type="PAYMENT_CAPTURED")) == 1


def test_idempotent_retry_returns_same_execution(env):
    order, view, token = env.granted()
    key = "retry-key-0001"
    first = env.core.execute(env.agent(), env.exec_input(view, order, token, idempotency_key=key))
    again = env.core.execute(env.agent(), env.exec_input(view, order, token, idempotency_key=key))
    assert again["execution"]["provider_order_id"] == first["execution"]["provider_order_id"]
    assert len(env.core.audit_events(event_type="PROVIDER_ORDER_CREATED")) == 1
    with pytest.raises(PactError) as e:  # same token, different key => replay
        env.core.execute(env.agent(), env.exec_input(view, order, token, idempotency_key="another-key-01"))
    assert e.value.code == "GRANT_ALREADY_USED"


@pytest.mark.parametrize("field,value", [("amount_minor", 100), ("merchant_id", "m_datahub"),
                                         ("currency", "EUR"), ("order_id", "ord_other")])
def test_modified_transaction_rejected_and_grant_still_usable(env, field, value):
    order, view, token = env.granted()
    with pytest.raises(PactError) as e:
        env.core.execute(env.agent(), env.exec_input(view, order, token, **{field: value}))
    assert e.value.code == "GRANT_BINDING_MISMATCH"
    assert events(env, view["request_id"])[-1] == "EXECUTION_REJECTED"
    ok = env.core.execute(env.agent(), env.exec_input(view, order, token))  # the honest call still works
    assert ok["state"] == "EXECUTING"


def test_order_changed_after_grant_is_rejected(env):
    order, view, token = env.granted()
    env.lab.update_order_items(order["order_id"], [{"product_id": env.product("Atlas")["product_id"], "quantity": 2}])
    with pytest.raises(PactError) as e:
        env.core.execute(env.agent(), env.exec_input(view, order, token))
    assert e.value.code == "ORDER_MODIFIED"


def test_wrong_token_and_wrong_agent(env):
    order, view, token = env.granted()
    with pytest.raises(PactError) as e:
        env.core.execute(env.agent(), env.exec_input(view, order, "pact_gr_forged"))
    assert e.value.code == "GRANT_INVALID"
    with pytest.raises(PactError) as e:  # another agent cannot even see the request
        env.core.execute(env.agent("ag_procurement"), env.exec_input(view, order, token))
    assert e.value.code == "REQUEST_NOT_FOUND"


def test_expired_grant_rejected_and_released(env):
    order, view, token = env.granted()
    env.clock.advance(seconds=601)
    with pytest.raises(PactError) as e:
        env.core.execute(env.agent(), env.exec_input(view, order, token))
    assert e.value.code == "GRANT_EXPIRED"
    assert env.core.get_status(env.agent(), view["request_id"])["state"] == "EXPIRED"
    assert org(env)["reserved_minor"] == 0


def test_revoked_grant_rejected(env):
    order, view, token = env.granted()
    env.core.admin_revoke("alice", view["request_id"])
    with pytest.raises(PactError) as e:
        env.core.execute(env.agent(), env.exec_input(view, order, token))
    assert e.value.code == "GRANT_REVOKED"


def test_concurrent_execution_with_one_grant(env):
    order, view, token = env.granted()
    agent = env.agent()
    outcomes, barrier = [], threading.Barrier(6)

    def run():
        barrier.wait()
        try:
            env.core.execute(agent, env.exec_input(view, order, token))
            outcomes.append("ok")
        except PactError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=run) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert outcomes.count("ok") == 1 and outcomes.count("GRANT_ALREADY_USED") == 5
    assert len(env.core.audit_events(event_type="PROVIDER_ORDER_CREATED")) == 1


def test_buyer_cancellation_releases(env):
    order, view, token, v = start(env)
    env.sim.buyer_cancel(v["execution"]["provider_order_id"])
    r = env.core.handle_provider_cancel(v["execution"]["provider_order_id"])
    assert r["state"] == "FAILED" and r["execution"]["failure_reason"] == "BUYER_CANCELLED"
    assert env.lab.get_order(order["order_id"])["payment_status"] == "CANCELLED"
    assert org(env)["reserved_minor"] == 0 and org(env)["settled_minor"] == 0


def test_cancel_redirect_after_capture_does_not_release(env):
    """A spoofed/late cancel must never release funds that were actually captured."""
    order, view, token, v = start(env)
    poid = v["execution"]["provider_order_id"]
    env.sim.buyer_approve(poid)
    env.sim.capture_order(poid, f"pact-{view['request_id']}-capture")  # captured at provider, PACT not told
    r = env.core.handle_provider_cancel(poid)
    assert r["state"] == "EXECUTED" and org(env)["settled_minor"] == 7900


def test_provider_failure_on_create_fails_and_releases(env):
    order, view, token = env.granted()
    env.sim.inject("create", "error")
    r = env.core.execute(env.agent(), env.exec_input(view, order, token))
    assert r["state"] == "FAILED" and r["execution"]["failure_reason"].startswith("PROVIDER_REJECTED")
    assert env.lab.get_order(order["order_id"])["payment_status"] == "FAILED"
    assert org(env)["reserved_minor"] == 0


def test_timeout_on_create_goes_unknown_then_reconciles_without_duplicate(env):
    order, view, token = env.granted()
    env.sim.inject("create", "timeout", after_side_effect=True)  # provider created it, we never heard back
    r = env.core.execute(env.agent(), env.exec_input(view, order, token))
    assert r["state"] == "UNKNOWN" and org(env)["reserved_minor"] == 7900  # funds stay reserved
    rec = env.core.reconcile(view["request_id"])
    assert rec["state"] == "EXECUTING" and rec["execution"]["approval_url"]
    assert len(env.sim._orders) == 1  # idempotency key returned the same provider order


def test_timeout_on_capture_after_side_effect_reconciles_once(env):
    order, view, token, v = start(env)
    poid = v["execution"]["provider_order_id"]
    env.sim.buyer_approve(poid)
    env.sim.inject("capture", "timeout", after_side_effect=True)
    r = env.core.handle_provider_return(poid)
    assert r["state"] == "UNKNOWN"
    assert org(env)["settled_minor"] == 0 and org(env)["reserved_minor"] == 7900
    s = env.core.get_status(env.agent(), view["request_id"])  # status poll triggers reconciliation
    assert s["state"] == "EXECUTED"
    assert org(env)["settled_minor"] == 7900 and org(env)["reserved_minor"] == 0
    assert len(env.core.audit_events(event_type="PAYMENT_CAPTURED")) == 1


def test_return_before_buyer_approval_keeps_waiting(env):
    order, view, token, v = start(env)
    r = env.core.handle_provider_return(v["execution"]["provider_order_id"])
    assert r["state"] == "EXECUTING" and r["execution"]["provider_status"] == "ORDER_NOT_APPROVED"
    assert org(env)["reserved_minor"] == 7900


def test_double_return_settles_once(env):
    order, view, token, v = start(env)
    poid = v["execution"]["provider_order_id"]
    env.sim.buyer_approve(poid)
    env.core.handle_provider_return(poid)
    env.core.handle_provider_return(poid)
    assert org(env)["settled_minor"] == 7900
    assert len(env.core.audit_events(event_type="PAYMENT_CAPTURED")) == 1


def test_status_poll_captures_after_buyer_approval(env):
    """If the browser never hits the return URL, polling status still completes the payment."""
    order, view, token, v = start(env)
    env.sim.buyer_approve(v["execution"]["provider_order_id"])
    assert env.core.get_status(env.agent(), view["request_id"])["state"] == "EXECUTED"


def test_unknown_return_token(env):
    with pytest.raises(PactError) as e:
        env.core.handle_provider_return("SIM-DOESNOTEXIST")
    assert e.value.code == "PROVIDER_ORDER_UNKNOWN"


def test_full_audit_trail(env):
    order, view, token, v = start(env)
    env.sim.buyer_approve(v["execution"]["provider_order_id"])
    env.core.handle_provider_return(v["execution"]["provider_order_id"])
    assert events(env, view["request_id"]) == [
        "REQUEST_CREATED", "BUDGET_RESERVED", "POLICY_EVALUATED", "APPROVAL_REQUESTED", "APPROVED",
        "GRANT_ISSUED", "GRANT_DELIVERED", "GRANT_CONSUMED", "EXECUTION_STARTED", "PROVIDER_ORDER_CREATED",
        "PAYMENT_CAPTURED"]
    detail = env.core.request_detail(view["request_id"])
    assert detail["merchant_name"] == "TechMart" and detail["order_snapshot"]["order_id"] == order["order_id"]
    assert "grant_token" not in detail and "token_hash" not in str(detail)
