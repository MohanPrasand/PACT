"""Simulation adapter: PayPal-like lifecycle, idempotency, fault injection, thread safety."""

from __future__ import annotations

import threading

import pytest

from adapters.simulation.adapter import SimulationAdapter
from pact_core.ports import CreatePayment, PaymentAdapter, ProviderError, ProviderTimeout


@pytest.fixture
def sim() -> SimulationAdapter:
    return SimulationAdapter("http://localhost:8000/")


def _req(key: str = "pact-req_1-create", **overrides) -> CreatePayment:
    base = dict(reference_id="req_1", merchant_order_id="ord_1", amount_minor=4200, currency="USD",
                description="Keyboard", return_url="http://localhost:8000/payments/return",
                cancel_url="http://localhost:8000/payments/cancel", idempotency_key=key)
    base.update(overrides)
    return CreatePayment(**base)


def _approved(sim: SimulationAdapter, key: str = "pact-req_1-create") -> str:
    oid = sim.create_order(_req(key)).provider_order_id
    sim.buyer_approve(oid)
    return oid


# ----------------------------------------------------------------------------- labelling


def test_is_labelled_simulation_never_paypal(sim):
    port: PaymentAdapter = sim
    assert port.name == "simulation"
    o = sim.create_order(_req())
    assert o.raw["provider"] == "simulation"
    assert "paypal" not in (o.approval_url or "").lower()
    assert "paypal" not in repr(o).lower()


# ----------------------------------------------------------------------------- lifecycle


def test_full_lifecycle(sim):
    o = sim.create_order(_req())
    assert o.status == "PAYER_ACTION_REQUIRED"
    assert o.provider_order_id.startswith("SIM-")
    assert o.approval_url == f"http://localhost:8000/sim/checkout/{o.provider_order_id}"
    assert o.capture_id is None and o.captured_amount_minor is None
    assert o.raw["reference_id"] == "req_1"

    snap = sim.buyer_approve(o.provider_order_id)
    assert snap.status == "APPROVED"
    assert sim.get_order(o.provider_order_id).status == "APPROVED"

    c = sim.capture_order(o.provider_order_id, "pact-req_1-capture")
    assert c.status == "COMPLETED"
    assert c.capture_status == "COMPLETED"
    assert c.capture_id and c.capture_id.startswith("SIMCAP-")
    assert c.captured_amount_minor == 4200 and c.captured_currency == "USD"
    assert c.approval_url is None

    g = sim.get_order(o.provider_order_id)
    assert (g.status, g.capture_id, g.captured_amount_minor) == ("COMPLETED", c.capture_id, 4200)


def test_buyer_approve_is_idempotent(sim):
    oid = sim.create_order(_req()).provider_order_id
    assert sim.buyer_approve(oid).status == "APPROVED"
    assert sim.buyer_approve(oid).status == "APPROVED"


def test_lookup_and_helpers_return_snapshots(sim):
    oid = sim.create_order(_req()).provider_order_id
    snap = sim.lookup(oid)
    assert (snap.amount_minor, snap.currency, snap.status) == (4200, "USD", "PAYER_ACTION_REQUIRED")
    assert snap.return_url.endswith("/payments/return") and snap.cancel_url.endswith("/payments/cancel")
    snap.status = "COMPLETED"  # mutating a snapshot must not change the order
    assert sim.get_order(oid).status == "PAYER_ACTION_REQUIRED"
    assert sim.lookup("SIM-NOPE") is None


def test_buyer_cancel_voids_order(sim):
    oid = sim.create_order(_req()).provider_order_id
    assert sim.buyer_cancel(oid).status == "VOIDED"
    assert sim.buyer_cancel(oid).status == "VOIDED"  # idempotent
    assert sim.get_order(oid).status == "VOIDED"
    with pytest.raises(ProviderError) as exc:
        sim.capture_order(oid, "k")
    assert exc.value.code == "ORDER_COMPLETED_OR_VOIDED"
    with pytest.raises(ProviderError) as exc:
        sim.buyer_approve(oid)
    assert exc.value.code == "ORDER_NOT_APPROVABLE"


def test_buyer_cancel_after_approval_voids(sim):
    oid = _approved(sim)
    assert sim.buyer_cancel(oid).status == "VOIDED"


def test_buyer_cannot_cancel_or_reapprove_captured_order(sim):
    oid = _approved(sim)
    sim.capture_order(oid, "k")
    with pytest.raises(ProviderError) as exc:
        sim.buyer_cancel(oid)
    assert exc.value.code == "ORDER_ALREADY_CAPTURED"
    with pytest.raises(ProviderError):
        sim.buyer_approve(oid)
    assert sim.get_order(oid).status == "COMPLETED"


@pytest.mark.parametrize("call", ["approve", "cancel", "capture", "get"])
def test_unknown_order(sim, call):
    fn = {"approve": lambda: sim.buyer_approve("SIM-X"), "cancel": lambda: sim.buyer_cancel("SIM-X"),
          "capture": lambda: sim.capture_order("SIM-X", "k"), "get": lambda: sim.get_order("SIM-X")}[call]
    with pytest.raises(ProviderError) as exc:
        fn()
    assert exc.value.code == "RESOURCE_NOT_FOUND"


# ----------------------------------------------------------------------------- idempotency & capture rules


def test_create_is_idempotent_per_key(sim):
    a = sim.create_order(_req("k1"))
    b = sim.create_order(_req("k1"))
    c = sim.create_order(_req("k2"))
    assert a.provider_order_id == b.provider_order_id
    assert c.provider_order_id != a.provider_order_id


def test_capture_before_approval_rejected(sim):
    oid = sim.create_order(_req()).provider_order_id
    with pytest.raises(ProviderError) as exc:
        sim.capture_order(oid, "cap-1")
    assert exc.value.code == "ORDER_NOT_APPROVED"
    assert sim.get_order(oid).status == "PAYER_ACTION_REQUIRED"
    # After approval the same key works (the failed attempt had no side effect).
    sim.buyer_approve(oid)
    assert sim.capture_order(oid, "cap-1").status == "COMPLETED"


def test_capture_is_idempotent_with_same_key(sim):
    oid = _approved(sim)
    first = sim.capture_order(oid, "cap-1")
    replay = sim.capture_order(oid, "cap-1")
    assert replay.capture_id == first.capture_id
    assert replay.status == "COMPLETED"


def test_double_capture_with_different_key_rejected(sim):
    oid = _approved(sim)
    sim.capture_order(oid, "cap-1")
    with pytest.raises(ProviderError) as exc:
        sim.capture_order(oid, "cap-2")
    assert exc.value.code == "ORDER_ALREADY_CAPTURED"


def test_create_key_does_not_double_as_capture_key(sim):
    oid = _approved(sim, key="shared-key")
    sim.capture_order(oid, "cap-1")
    with pytest.raises(ProviderError) as exc:
        sim.capture_order(oid, "shared-key")
    assert exc.value.code == "ORDER_ALREADY_CAPTURED"


def test_capture_key_scoped_to_order(sim):
    a = _approved(sim, key="ka")
    b = _approved(sim, key="kb")
    ca = sim.capture_order(a, "same-cap-key")
    cb = sim.capture_order(b, "same-cap-key")
    assert ca.capture_id != cb.capture_id
    assert sim.get_order(b).status == "COMPLETED"


@pytest.mark.parametrize("overrides,code", [
    ({"currency": "JPY"}, "CURRENCY_NOT_SUPPORTED"),
    ({"currency": "usd"}, "CURRENCY_NOT_SUPPORTED"),
    ({"amount_minor": 0}, "INVALID_AMOUNT"),
    ({"amount_minor": -1}, "INVALID_AMOUNT"),
    ({"idempotency_key": ""}, "INVALID_REQUEST_ID"),
])
def test_invalid_create_requests_rejected(sim, overrides, code):
    with pytest.raises(ProviderError) as exc:
        sim.create_order(_req(**overrides))
    assert exc.value.code == code


def test_capture_requires_key(sim):
    oid = _approved(sim)
    with pytest.raises(ProviderError) as exc:
        sim.capture_order(oid, "")
    assert exc.value.code == "INVALID_REQUEST_ID"
    assert sim.get_order(oid).status == "APPROVED"


# ----------------------------------------------------------------------------- fault injection


def test_create_timeout_before_side_effect_creates_nothing(sim):
    sim.inject("create", "timeout")
    with pytest.raises(ProviderTimeout):
        sim.create_order(_req("k1"))
    assert sim._orders == {}
    # One-shot: the retry succeeds and creates exactly one order.
    o = sim.create_order(_req("k1"))
    assert len(sim._orders) == 1 and o.status == "PAYER_ACTION_REQUIRED"


def test_create_timeout_after_side_effect_leaves_order(sim):
    sim.inject("create", "timeout", after_side_effect=True)
    with pytest.raises(ProviderTimeout):
        sim.create_order(_req("k1"))
    assert len(sim._orders) == 1
    # Retrying with the same idempotency key returns the order that was created, not a second one.
    o = sim.create_order(_req("k1"))
    assert len(sim._orders) == 1
    assert o.provider_order_id == next(iter(sim._orders))


def test_capture_timeout_before_side_effect(sim):
    oid = _approved(sim)
    sim.inject("capture", "timeout")
    with pytest.raises(ProviderTimeout):
        sim.capture_order(oid, "cap-1")
    assert sim.get_order(oid).status == "APPROVED"  # reconciliation: not captured
    assert sim.capture_order(oid, "cap-1").status == "COMPLETED"


def test_capture_timeout_after_side_effect_requires_reconciliation(sim):
    """The dangerous case: the caller sees a timeout but the money moved."""
    oid = _approved(sim)
    sim.inject("capture", "timeout", after_side_effect=True)
    with pytest.raises(ProviderTimeout):
        sim.capture_order(oid, "cap-1")
    g = sim.get_order(oid)  # reconciliation reveals the capture
    assert g.status == "COMPLETED" and g.capture_status == "COMPLETED" and g.captured_amount_minor == 4200
    # Retrying with the same key replays (no double capture); a new key is refused.
    assert sim.capture_order(oid, "cap-1").capture_id == g.capture_id
    with pytest.raises(ProviderError) as exc:
        sim.capture_order(oid, "cap-2")
    assert exc.value.code == "ORDER_ALREADY_CAPTURED"


def test_capture_error_injection_with_custom_code(sim):
    oid = _approved(sim)
    sim.inject("capture", "error", code="INSTRUMENT_DECLINED")
    with pytest.raises(ProviderError) as exc:
        sim.capture_order(oid, "cap-1")
    assert exc.value.code == "INSTRUMENT_DECLINED" and not exc.value.retryable
    assert sim.get_order(oid).status == "APPROVED"


def test_error_after_side_effect(sim):
    sim.inject("create", "error", after_side_effect=True)
    with pytest.raises(ProviderError) as exc:
        sim.create_order(_req("k1"))
    assert exc.value.code == "SIMULATED_FAILURE"
    assert len(sim._orders) == 1


def test_get_fault(sim):
    oid = sim.create_order(_req()).provider_order_id
    sim.inject("get", "timeout")
    with pytest.raises(ProviderTimeout):
        sim.get_order(oid)
    sim.inject("get", "error")
    with pytest.raises(ProviderError):
        sim.get_order(oid)
    assert sim.get_order(oid).status == "PAYER_ACTION_REQUIRED"


def test_faults_are_consumed_in_order_and_per_operation(sim):
    sim.inject("capture", "timeout")
    sim.inject("create", "error")
    sim.inject("create", "timeout")
    with pytest.raises(ProviderError):
        sim.create_order(_req("k1"))
    with pytest.raises(ProviderTimeout):
        sim.create_order(_req("k1"))
    oid = sim.create_order(_req("k1")).provider_order_id
    sim.buyer_approve(oid)
    with pytest.raises(ProviderTimeout):
        sim.capture_order(oid, "c")
    assert sim.capture_order(oid, "c").status == "COMPLETED"


def test_clear_faults(sim):
    sim.inject("create", "timeout")
    sim.clear_faults()
    assert sim.create_order(_req()).status == "PAYER_ACTION_REQUIRED"


@pytest.mark.parametrize("args,kwargs", [
    (("refund", "timeout"), {}),
    (("create", "explode"), {}),
    (("get", "timeout"), {"after_side_effect": True}),
])
def test_inject_validates_arguments(sim, args, kwargs):
    with pytest.raises(ValueError):
        sim.inject(*args, **kwargs)


# ----------------------------------------------------------------------------- concurrency


def _run_threads(n: int, target) -> list:
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        barrier.wait()
        try:
            results.append(target(i))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_concurrent_create_with_same_key_yields_one_order(sim):
    results, errors = _run_threads(32, lambda i: sim.create_order(_req("same-key")).provider_order_id)
    assert not errors
    assert len(set(results)) == 1 and len(results) == 32
    assert len(sim._orders) == 1


def test_concurrent_capture_with_different_keys_captures_once(sim):
    oid = _approved(sim)
    results, errors = _run_threads(32, lambda i: sim.capture_order(oid, f"cap-{i}").capture_id)
    assert len(results) == 1
    assert len(errors) == 31 and all(isinstance(e, ProviderError) and e.code == "ORDER_ALREADY_CAPTURED"
                                     for e in errors)


def test_concurrent_capture_with_same_key_all_see_same_capture(sim):
    oid = _approved(sim)
    results, errors = _run_threads(16, lambda i: sim.capture_order(oid, "cap").capture_id)
    assert not errors and len(set(results)) == 1


def test_concurrent_fault_injection_fires_once(sim):
    sim.inject("create", "timeout")
    results, errors = _run_threads(16, lambda i: sim.create_order(_req(f"k{i}")).provider_order_id)
    assert len(errors) == 1 and isinstance(errors[0], ProviderTimeout)
    assert len(results) == 15
