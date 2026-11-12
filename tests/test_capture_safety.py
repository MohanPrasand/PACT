"""Money-safety regressions found by the independent review of the PayPal gateway fixes (#38-#42).

Rule under test: while a capture may have happened (a capture call is in flight or timed out, a capture is
pending, or the provider showed a capture PACT could not settle), nothing releases the budget reservation on
indirect evidence: not an order lookup (which can lag behind a capture), not "order not found", not a buyer
or operator cancel, not the checkout timeout. Only the provider's answer to the capture itself decides,
or an operator's explicit, audited force."""

from __future__ import annotations

import pytest

from pact_core.errors import PactError
from pact_core.ports import ProviderError, ProviderOrder, ProviderTimeout
from tests.pact_env import build_env


@pytest.fixture
def env(db, clock):
    return build_env(db, clock)


def events(env, rid):
    return [e["event_type"] for e in reversed(env.core.audit_events(request_id=rid, limit=500))]


def org(env):
    return next(s for s in env.core.budget_status()["scopes"] if s["scope"] == "org:global")


def state_of(env, rid):
    with env.core.db.transaction() as s:
        req = env.core.ctx.load_request(s, rid)
        return req.state, req.failure_reason


def consistent(env):
    assert env.core.verify_audit()["valid"]
    with env.core.db.transaction() as s:
        assert env.core.ctx.budget.check_invariants(s) == []


def start(env):
    order, view, token = env.granted("Atlas")
    v = env.core.execute(env.agent(), env.exec_input(view, order, token))
    return order, v["execution"]["provider_order_id"], view["request_id"]


def gone(_poid):
    raise ProviderError("INVALID_RESOURCE_ID", "Specified resource ID does not exist")


# ---------------------------------------------------------------- cancel after a capture of unknown outcome

@pytest.mark.parametrize("who", ["buyer", "operator"])
def test_cancel_after_unknown_capture_settles_instead_of_releasing(env, monkeypatch, who):
    """The capture timed out after the provider took the money, and order lookups still say APPROVED."""
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    env.sim.inject("capture", "timeout", after_side_effect=True)
    assert env.core.handle_provider_return(poid)["state"] == "UNKNOWN"
    monkeypatch.setattr(env.sim, "get_order", lambda p: ProviderOrder(provider_order_id=p, status="APPROVED"))

    r = env.core.buyer_abandon(poid) if who == "buyer" else env.core.cancel_payment("alice", rid)

    assert r["state"] == "EXECUTED"  # the capture was asked again with the same key and answered
    assert org(env)["settled_minor"] == 7900 and org(env)["reserved_minor"] == 0
    assert env.lab.get_order(order["order_id"])["payment_status"] == "CAPTURED"
    assert "EXECUTION_FAILED" not in events(env, rid)
    consistent(env)


def test_cancel_is_refused_while_the_unknown_capture_cannot_be_settled(env, monkeypatch):
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    monkeypatch.setattr(env.sim, "capture_order", lambda p, k: (_ for _ in ()).throw(ProviderTimeout("lost")))
    assert env.core.handle_provider_return(poid)["state"] == "UNKNOWN"
    monkeypatch.setattr(env.sim, "get_order", lambda p: ProviderOrder(provider_order_id=p, status="APPROVED"))
    for cancel in (lambda: env.core.buyer_abandon(poid), lambda: env.core.cancel_payment("alice", rid)):
        with pytest.raises(PactError) as e:
            cancel()
        assert e.value.code == "CANNOT_VERIFY_PROVIDER"
    env.clock.advance(seconds=7200)
    assert env.core.expire_stale() == []
    assert state_of(env, rid) == ("UNKNOWN", "PROVIDER_TIMEOUT_ON_CAPTURE") and org(env)["reserved_minor"] == 7900
    consistent(env)


# ---------------------------------------------------------------- cancel racing a capture in flight

def test_cancel_cannot_overtake_a_capture_in_flight(env, monkeypatch):
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    real, seen = env.sim.capture_order, []

    def capture(p, key):  # the capture is at the provider when the cancels arrive
        for cancel in (lambda: env.core.buyer_abandon(poid), lambda: env.core.cancel_payment("alice", rid),
                       lambda: env.core.cancel_payment("alice", rid, force=True)):
            with pytest.raises(PactError) as e:
                cancel()
            seen.append(e.value.code)
        assert state_of(env, rid) == ("EXECUTING", "CAPTURE_IN_PROGRESS")
        return real(p, key)

    monkeypatch.setattr(env.sim, "capture_order", capture)
    r = env.core.handle_provider_return(poid)
    assert seen == ["CANNOT_VERIFY_PROVIDER"] * 3
    assert r["state"] == "EXECUTED" and org(env)["settled_minor"] == 7900
    assert state_of(env, rid) == ("EXECUTED", None)
    assert "EXECUTION_FAILED" not in events(env, rid) and "CAPTURE_AFTER_TERMINAL_STATE" not in events(env, rid)
    consistent(env)


def test_fail_itself_refuses_when_a_capture_marker_is_set(env):
    """The last line of defence, independent of the callers' own checks."""
    order, poid, rid = start(env)
    with env.core.db.transaction() as s:
        env.core.ctx.load_request(s, rid).failure_reason = "CAPTURE_IN_PROGRESS"  # e.g. left by a crash mid-call
    r = env.core.gateway._fail(rid, "ANYTHING")
    assert r["state"] == "EXECUTING" and org(env)["reserved_minor"] == 7900
    rejected = env.core.audit_events(request_id=rid, event_type="EXECUTION_REJECTED")
    assert rejected and rejected[0]["data"]["code"] == "CAPTURE_IN_DOUBT"
    consistent(env)


def test_marker_left_by_a_crash_is_settled_by_the_next_poll(env):
    order, poid, rid = start(env)
    with env.core.db.transaction() as s:
        env.core.ctx.load_request(s, rid).failure_reason = "CAPTURE_IN_PROGRESS"
    r = env.core.reconcile(rid)  # buyer never approved: the capture is asked again and answers "not approved"
    assert r["state"] == "EXECUTING" and state_of(env, rid) == ("EXECUTING", None)
    env.clock.advance(seconds=7200)
    assert env.core.expire_stale() == [rid] and state_of(env, rid) == ("FAILED", "CHECKOUT_TIMEOUT")
    consistent(env)


# ---------------------------------------------------------------- "order not found" after a capture was seen

@pytest.mark.parametrize("po", [
    dict(status="COMPLETED", capture_id="C1", capture_status="COMPLETED", captured_amount_minor=1, captured_currency="USD"),
    dict(status="COMPLETED", capture_id="C1", capture_status="PENDING", captured_amount_minor=7900, captured_currency="USD"),
    dict(status="COMPLETED"),
], ids=["amount-mismatch", "capture-pending", "status-unclear"])
def test_order_not_found_never_releases_once_a_capture_was_seen(env, monkeypatch, po):
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    monkeypatch.setattr(env.sim, "capture_order", lambda p, k: ProviderOrder(provider_order_id=p, **po))
    before = env.core.handle_provider_return(poid)["state"]
    monkeypatch.setattr(env.sim, "get_order", gone)

    for _ in range(3):
        assert env.core.reconcile(rid)["state"] == before
    assert env.core.handle_provider_cancel(poid)["state"] == before
    with pytest.raises(PactError) as e:
        env.core.cancel_payment("alice", rid)
    assert e.value.code == "CANNOT_VERIFY_PROVIDER"
    env.clock.advance(seconds=7200)
    assert env.core.expire_stale() == []
    assert org(env)["reserved_minor"] == 7900 and "EXECUTION_FAILED" not in events(env, rid)
    held = [x for x in env.core.audit_events(request_id=rid, event_type="RECONCILED")
            if x["data"].get("action") == "hold_unknown_capture_order_not_found"]
    assert len(held) == 1  # audited once, not on every poll
    consistent(env)


def test_operator_force_is_the_audited_exit_from_manual_review(env, monkeypatch):
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    monkeypatch.setattr(env.sim, "capture_order", lambda p, k: (_ for _ in ()).throw(ProviderTimeout("lost")))
    env.core.handle_provider_return(poid)
    monkeypatch.setattr(env.sim, "get_order", gone)  # the provider no longer answers for this order
    assert env.core.reconcile(rid)["state"] == "UNKNOWN"
    with pytest.raises(PactError):
        env.core.cancel_payment("alice", rid)

    r = env.core.cancel_payment("alice", rid, force=True)
    assert r["state"] == "FAILED" and r["execution"]["failure_reason"] == "FORCE_CANCELLED"
    assert org(env)["reserved_minor"] == 0
    forced = env.core.audit_events(request_id=rid, event_type="CANCEL_FORCED")
    assert len(forced) == 1 and forced[0]["actor"] == "admin:alice" and forced[0]["data"]["capture_in_doubt"] is True
    consistent(env)


# ---------------------------------------------------------------- loops and stuck states

def test_decline_while_unknown_answers_the_capture_and_polls_do_not_loop(env, monkeypatch):
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    keys = []

    def capture(p, key):
        keys.append(key)
        if len(keys) == 1:
            raise ProviderTimeout("5xx")
        raise ProviderError("INSTRUMENT_DECLINED", "declined")

    monkeypatch.setattr(env.sim, "capture_order", capture)
    assert env.core.handle_provider_return(poid)["state"] == "UNKNOWN"
    for _ in range(10):
        env.clock.advance(seconds=3)
        s = env.core.get_status(env.agent(), rid)
    # One retry with the SAME key answered the unknown call; after that polls wait for the buyer.
    assert keys == [f"pact-{rid}-capture"] * 2
    assert s["state"] == "EXECUTING" and s["execution"]["provider_status"] == "INSTRUMENT_DECLINED"
    assert s["execution"]["failure_reason"] is None

    for _ in range(200):  # an hour of polling: a few spaced retries, then none
        env.clock.advance(seconds=20)
        env.core.get_status(env.agent(), rid)
    assert len(keys) == 5 and len(set(keys)) == 4
    assert org(env)["reserved_minor"] == 7900
    consistent(env)


def test_buyer_who_reapproves_without_returning_is_captured_by_a_later_poll(env, monkeypatch):
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    real, keys = env.sim.capture_order, []

    def capture(p, key):
        keys.append(key)
        if len(keys) == 1:
            raise ProviderError("INSTRUMENT_DECLINED", "declined")
        return real(p, key)  # the buyer chose another funding source at the provider and closed the tab

    monkeypatch.setattr(env.sim, "capture_order", capture)
    env.core.handle_provider_return(poid)
    env.clock.advance(seconds=30)
    assert env.core.get_status(env.agent(), rid)["state"] == "EXECUTING" and len(keys) == 1  # too soon
    env.clock.advance(seconds=40)
    assert env.core.get_status(env.agent(), rid)["state"] == "EXECUTED"
    assert keys == [f"pact-{rid}-capture", f"pact-{rid}-capture-1"]
    consistent(env)


def test_early_return_during_a_provider_blip_does_not_strand_the_payment(env):
    """Anyone can GET the return URL before the buyer approves. If that capture call times out, the next poll
    asks the capture again, learns "not approved", and the payment is an ordinary open checkout again."""
    order, poid, rid = start(env)
    env.sim.inject("capture", "timeout")
    assert env.core.handle_provider_return(poid)["state"] == "UNKNOWN"
    s = env.core.get_status(env.agent(), rid)
    assert s["state"] == "EXECUTING" and s["execution"]["failure_reason"] is None
    env.clock.advance(seconds=7200)
    assert env.core.expire_stale() == [rid]
    assert state_of(env, rid) == ("FAILED", "CHECKOUT_TIMEOUT") and org(env)["reserved_minor"] == 0
    consistent(env)


def test_cancel_link_during_a_provider_outage_changes_nothing(env):
    order, poid, rid = start(env)
    env.sim.inject("get", "timeout")
    r = env.core.handle_provider_cancel(poid)
    assert r["state"] == "EXECUTING" and r["execution"]["failure_reason"] is None
    assert events(env, rid)[-1] == "PROVIDER_ORDER_CREATED"  # nothing was learned, nothing was written
    env.clock.advance(seconds=7200)
    assert env.core.expire_stale() == [rid] and org(env)["reserved_minor"] == 0  # the timeout still reaches it
    consistent(env)


def test_amount_mismatch_is_audited_once(env, monkeypatch):
    order, poid, rid = start(env)
    env.sim.buyer_approve(poid)
    bad = lambda p, *a: ProviderOrder(provider_order_id=p, status="COMPLETED", capture_id="C1",  # noqa: E731
                                      capture_status="COMPLETED", captured_amount_minor=1, captured_currency="USD")
    monkeypatch.setattr(env.sim, "capture_order", bad)
    monkeypatch.setattr(env.sim, "get_order", bad)
    env.core.handle_provider_return(poid)
    for _ in range(10):
        env.clock.advance(seconds=3)
        env.core.get_status(env.agent(), rid)
    assert events(env, rid).count("CAPTURE_MISMATCH") == 1
    assert state_of(env, rid) == ("UNKNOWN", "CAPTURE_AMOUNT_MISMATCH") and org(env)["reserved_minor"] == 7900
    consistent(env)


def test_recovered_create_timeout_leaves_no_stale_reason(env):
    order, view, token = env.granted("Atlas")
    env.sim.inject("create", "timeout", after_side_effect=True)
    v = env.core.execute(env.agent(), env.exec_input(view, order, token))
    rid = view["request_id"]
    assert v["state"] == "UNKNOWN" and v["execution"]["failure_reason"] == "PROVIDER_TIMEOUT_ON_CREATE"
    r = env.core.reconcile(rid)  # the create is repeated with the same key and answers
    assert r["state"] == "EXECUTING" and r["execution"]["failure_reason"] is None
    env.clock.advance(seconds=7200)
    assert env.core.expire_stale() == [rid] and state_of(env, rid) == ("FAILED", "CHECKOUT_TIMEOUT")
    consistent(env)
