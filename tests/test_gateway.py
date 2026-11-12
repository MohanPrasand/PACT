"""#10 Execution gateway: validation chain, idempotency, provider lifecycle, UNKNOWN and reconciliation.
Uses the simulation adapter (spec §14.4); PayPal-specific parsing is covered in test_paypal_adapter.py."""

from __future__ import annotations

import threading

import httpx
import pytest

from adapters.paypal.adapter import PayPalAdapter
from merchant_lab.service import MerchantLabOrderSource
from pact_core.core import PactCore
from pact_core.errors import PactError
from pact_core.gateway import NOT_FOUND
from pact_core.ports import ProviderError
from tests.pact_env import PUBLIC_URL, build_env


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


# ---------------------------------------------------------------------- #38 / #39 helpers
PAYPAL_ORDER_ID = "5O190127TN364715T"
# What PayPal Sandbox answers for a missing or expired order (HTTP 404).
PAYPAL_404 = {
    "name": "RESOURCE_NOT_FOUND",
    "message": "The specified resource does not exist.",
    "debug_id": "f1a2b3c4d5e6f",
    "details": [{"issue": "INVALID_RESOURCE_ID", "description": "Specified resource ID does not exist."}],
    "links": [{"href": "https://developer.paypal.com/docs/api/orders/v2/#error-INVALID_RESOURCE_ID",
               "rel": "information_link"}],
}


def assert_consistent(env):
    assert env.core.verify_audit()["valid"]
    with env.core.db.transaction() as s:
        assert env.core.ctx.budget.check_invariants(s) == []


def count_calls(env, monkeypatch):
    """Count adapter calls made by the gateway from here on."""
    calls = {"capture_order": 0, "get_order": 0}
    for name in calls:
        def counted(*a, _name=name, _real=getattr(env.sim, name), **kw):
            calls[_name] += 1
            return _real(*a, **kw)
        monkeypatch.setattr(env.sim, name, counted)
    return calls


def reject_capture(env, monkeypatch, code, *, retryable=False):
    """Every capture is refused with ``code`` while the order stays APPROVED at the provider."""
    def refuse(provider_order_id, idempotency_key):
        raise ProviderError(code, f"simulated {code}", retryable=retryable)
    monkeypatch.setattr(env.sim, "capture_order", refuse)


def order_gone(env, monkeypatch, code="INVALID_RESOURCE_ID"):
    """The provider no longer knows the order (PayPal: expired or never existed)."""
    def gone(provider_order_id):
        raise ProviderError(code, "Specified resource ID does not exist.")
    monkeypatch.setattr(env.sim, "get_order", gone)


def _paypal_adapter(handler) -> PayPalAdapter:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AA-test-token", "token_type": "Bearer",
                                             "expires_in": 32400})
        return handler(request)
    return PayPalAdapter("test-client-id", "test-client-secret", transport=httpx.MockTransport(route))


@pytest.fixture
def paypal_env(db, clock):
    """Gateway wired to the real PayPalAdapter over httpx.MockTransport: create succeeds, every other call
    gets PayPal's 404 body. ``env.sim`` is unused here."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/checkout/orders":
            return httpx.Response(201, json={
                "id": PAYPAL_ORDER_ID, "status": "PAYER_ACTION_REQUIRED",
                "links": [{"rel": "payer-action", "method": "GET",
                           "href": f"https://www.sandbox.paypal.com/checkoutnow?token={PAYPAL_ORDER_ID}"}]})
        return httpx.Response(404, json=PAYPAL_404)

    env = build_env(db, clock)
    adapter = _paypal_adapter(handler)
    env.core = PactCore(db, MerchantLabOrderSource(env.lab), {"paypal": adapter}, provider="paypal", currency="USD",
                        public_url=PUBLIC_URL, grant_ttl_seconds=600, approval_ttl_seconds=1800, clock=clock)
    yield env
    adapter.close()


# ---------------------------------------------------------------------- #38 provider order not found
def test_paypal_404_body_maps_to_invalid_resource_id():
    adapter = _paypal_adapter(lambda request: httpx.Response(404, json=PAYPAL_404))
    with pytest.raises(ProviderError) as e:
        adapter.get_order(PAYPAL_ORDER_ID)
    assert e.value.code == "INVALID_RESOURCE_ID" and e.value.code in NOT_FOUND and not e.value.retryable
    adapter.close()


def test_paypal_order_not_found_fails_and_releases(paypal_env):
    """#38: PayPal's 404 reaches the gateway as INVALID_RESOURCE_ID, not the simulation's RESOURCE_NOT_FOUND."""
    env = paypal_env
    order, view, token, v = start(env)
    assert v["state"] == "EXECUTING" and v["execution"]["provider_order_id"] == PAYPAL_ORDER_ID
    assert org(env)["reserved_minor"] == 7900

    r = env.core.gateway.reconcile(view["request_id"])
    assert r["state"] == "FAILED" and r["execution"]["failure_reason"] == "PROVIDER_ORDER_NOT_FOUND"
    assert org(env)["reserved_minor"] == 0 and org(env)["settled_minor"] == 0
    assert env.lab.get_order(order["order_id"])["payment_status"] == "FAILED"
    assert events(env, view["request_id"])[-1] == "EXECUTION_FAILED"
    assert_consistent(env)


def test_status_poll_fails_request_when_provider_order_is_gone(env, monkeypatch):
    order, view, token, v = start(env)
    order_gone(env, monkeypatch)
    s = env.core.get_status(env.agent(), view["request_id"])
    assert s["state"] == "FAILED" and s["execution"]["failure_reason"] == "PROVIDER_ORDER_NOT_FOUND"
    assert org(env)["reserved_minor"] == 0
    assert_consistent(env)


def test_abandon_succeeds_when_provider_order_is_gone(paypal_env):
    env = paypal_env
    order, view, token, v = start(env)
    r = env.core.gateway.abandon(view["request_id"], "admin")
    assert r["state"] == "FAILED" and r["execution"]["failure_reason"] == "ABANDONED_CHECKOUT"
    assert env.lab.get_order(order["order_id"])["payment_status"] == "CANCELLED"
    assert org(env)["reserved_minor"] == 0 and org(env)["settled_minor"] == 0
    assert "CANCEL_REQUESTED" in events(env, view["request_id"])
    assert_consistent(env)


def test_buyer_cancel_releases_when_provider_order_is_gone(env, monkeypatch):
    order, view, token, v = start(env)
    order_gone(env, monkeypatch)
    r = env.core.handle_provider_cancel(v["execution"]["provider_order_id"])
    assert r["state"] == "FAILED" and r["execution"]["failure_reason"] == "BUYER_CANCELLED"
    assert org(env)["reserved_minor"] == 0
    assert_consistent(env)


def test_order_not_found_after_unknown_capture_stays_unknown(env, monkeypatch):
    """Money safety: a capture call with an unknown outcome may have moved money, so a 404 never releases."""
    order, view, token, v = start(env)
    poid, rid = v["execution"]["provider_order_id"], view["request_id"]
    env.sim.buyer_approve(poid)
    env.sim.inject("capture", "timeout", after_side_effect=True)
    r = env.core.handle_provider_return(poid)
    assert r["state"] == "UNKNOWN" and r["execution"]["failure_reason"] == "PROVIDER_TIMEOUT_ON_CAPTURE"

    order_gone(env, monkeypatch)
    for _ in range(2):  # repeated polls neither release nor spam the audit log
        r = env.core.gateway.reconcile(rid)
        assert r["state"] == "UNKNOWN" and r["execution"]["failure_reason"] == "PROVIDER_TIMEOUT_ON_CAPTURE"
    assert env.core.get_status(env.agent(), rid)["state"] == "UNKNOWN"
    held = [e for e in env.core.audit_events(request_id=rid, event_type="RECONCILED")
            if "INVALID_RESOURCE_ID" in str(e)]
    assert len(held) == 1

    with pytest.raises(PactError) as e:  # the operator cannot cancel it either
        env.core.gateway.abandon(rid, "admin")
    assert e.value.code == "CANNOT_VERIFY_PROVIDER"
    r = env.core.handle_provider_cancel(poid)  # nor can a cancel redirect
    assert r["state"] == "UNKNOWN" and r["execution"]["failure_reason"] == "PROVIDER_TIMEOUT_ON_CAPTURE"

    assert org(env)["reserved_minor"] == 7900 and org(env)["settled_minor"] == 0
    assert "EXECUTION_FAILED" not in events(env, rid)
    assert_consistent(env)


# ---------------------------------------------------------------------- #39 capture rejected
def test_persistent_capture_rejection_fails_without_recursion(env, monkeypatch):
    """#39: capture keeps being refused while the order stays APPROVED. One capture, one lookup, then FAILED."""
    order, view, token, v = start(env)
    poid = v["execution"]["provider_order_id"]
    env.sim.buyer_approve(poid)
    reject_capture(env, monkeypatch, "PAYER_CANNOT_PAY")
    calls = count_calls(env, monkeypatch)

    r = env.core.handle_provider_return(poid)
    assert r["state"] == "FAILED" and r["execution"]["failure_reason"] == "CAPTURE_REJECTED:PAYER_CANNOT_PAY"
    assert calls == {"capture_order": 1, "get_order": 1}
    assert env.sim.lookup(poid).status == "APPROVED"  # the provider never captured
    assert env.lab.get_order(order["order_id"])["payment_status"] == "FAILED"
    assert org(env)["reserved_minor"] == 0 and org(env)["settled_minor"] == 0

    env.core.get_status(env.agent(), view["request_id"])  # terminal: later polls call the provider no more
    assert calls == {"capture_order": 1, "get_order": 1}
    assert_consistent(env)


def test_capture_rejected_during_status_poll_fails_without_recursion(env, monkeypatch):
    """The poll path is reconcile -> capture; a refusal there must not loop back into reconcile."""
    order, view, token, v = start(env)
    env.sim.buyer_approve(v["execution"]["provider_order_id"])
    reject_capture(env, monkeypatch, "PAYER_CANNOT_PAY")
    calls = count_calls(env, monkeypatch)
    s = env.core.get_status(env.agent(), view["request_id"])
    assert s["state"] == "FAILED" and s["execution"]["failure_reason"] == "CAPTURE_REJECTED:PAYER_CANNOT_PAY"
    assert calls == {"capture_order": 1, "get_order": 2}  # reconcile's lookup + one after the refusal
    assert org(env)["reserved_minor"] == 0
    assert_consistent(env)


def test_order_already_captured_finalizes(env):
    """The capture happened earlier (different request id): the refusal must settle, never release."""
    order, view, token, v = start(env)
    poid = v["execution"]["provider_order_id"]
    env.sim.buyer_approve(poid)
    env.sim.capture_order(poid, "some-other-capture-key")  # captured at the provider, PACT not told
    r = env.core.handle_provider_return(poid)  # -> ORDER_ALREADY_CAPTURED
    assert r["state"] == "EXECUTED" and r["execution"]["capture_id"].startswith("SIMCAP-")
    assert org(env)["settled_minor"] == 7900 and org(env)["reserved_minor"] == 0
    assert env.lab.get_order(order["order_id"])["payment_status"] == "CAPTURED"
    assert len(env.core.audit_events(event_type="PAYMENT_CAPTURED")) == 1
    assert_consistent(env)


def test_retryable_capture_error_stays_executing(env, monkeypatch):
    """A 429 on capture is transient: keep the reservation and let the next poll try again."""
    order, view, token, v = start(env)
    poid = v["execution"]["provider_order_id"]
    env.sim.buyer_approve(poid)
    real_capture = env.sim.capture_order
    reject_capture(env, monkeypatch, "RATE_LIMIT_REACHED", retryable=True)
    calls = count_calls(env, monkeypatch)

    r = env.core.handle_provider_return(poid)
    assert r["state"] == "EXECUTING" and r["execution"]["provider_status"] == "RATE_LIMIT_REACHED"
    assert r["execution"]["failure_reason"] is None
    assert calls == {"capture_order": 1, "get_order": 1}
    assert org(env)["reserved_minor"] == 7900 and org(env)["settled_minor"] == 0
    assert_consistent(env)

    monkeypatch.setattr(env.sim, "capture_order", real_capture)  # the rate limit clears
    assert env.core.get_status(env.agent(), view["request_id"])["state"] == "EXECUTED"
    assert org(env)["settled_minor"] == 7900 and org(env)["reserved_minor"] == 0
    assert_consistent(env)


def test_capture_rejected_but_lookup_fails_keeps_reservation(env, monkeypatch):
    """If the follow-up lookup cannot be made, nothing is decided: no release, no second capture."""
    order, view, token, v = start(env)
    poid = v["execution"]["provider_order_id"]
    env.sim.buyer_approve(poid)
    reject_capture(env, monkeypatch, "PAYER_CANNOT_PAY")
    calls = count_calls(env, monkeypatch)
    env.sim.inject("get", "timeout")
    r = env.core.handle_provider_return(poid)
    assert r["state"] == "EXECUTING" and r["execution"]["provider_status"] == "PAYER_CANNOT_PAY"
    assert calls == {"capture_order": 1, "get_order": 1}
    assert org(env)["reserved_minor"] == 7900
    assert_consistent(env)
