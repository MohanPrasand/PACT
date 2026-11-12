"""#24/#25 backend: merchant-safe order status and human web checkout governed by PACT (ag_storefront)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.seed import seed_catalogue
from pact_core.config import Settings
from pact_core.seed import seed_agents, seed_policies
from pact_server.app import create_app

ADMIN = "test-admin-token-0123456789abcdef"
PUBLIC = "http://testserver"
ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
FORBIDDEN_KEYS = {"grant_token", "token_hash", "request_hash", "api_key", "api_key_hash", "client_secret"}


@pytest.fixture
def web(db, clock):
    settings = Settings(_env_file=None, PACT_ADMIN_TOKEN=ADMIN, PACT_PUBLIC_URL=PUBLIC,
                        PACT_PAYMENT_PROVIDER="simulation", PACT_DATABASE_URL="sqlite://")
    app = create_app(settings, adapters={"simulation": SimulationAdapter(PUBLIC)}, clock=clock,
                     start_sweeper=False, db=db)
    seed_catalogue(db)
    seed_policies(db)
    keys = seed_agents(db)
    client = TestClient(app)
    client.keys = keys
    return client


def order_for(web, *, merchant="m_techmart", cheap=False, buyer="web_test"):
    params = {"merchant_id": merchant, "sort": "price"}
    products = web.get("/merchant/products", params=params).json()["products"]
    pick = products[0] if cheap else next(p for p in products if p["price_minor"] >= 7000)
    r = web.post("/merchant/orders", json={"items": [{"product_id": pick["product_id"], "quantity": 1}],
                                           "buyer_ref": buyer})
    assert r.status_code == 201
    return r.json()


def assert_no_secrets(payload):
    def walk(x):
        if isinstance(x, dict):
            assert not (FORBIDDEN_KEYS & set(x)), f"secret-like field in {sorted(FORBIDDEN_KEYS & set(x))}"
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(payload)


def test_storefront_agent_is_seeded(web):
    agents = {a["agent_id"] for a in web.get("/admin/agents", headers=ADMIN_H).json()["agents"]}
    assert "ag_storefront" in agents


def test_status_before_checkout(web):
    order = order_for(web)
    s = web.get(f"/merchant/orders/{order['order_id']}/status").json()
    assert s["payment_status"] == "CREATED" and s["authorization"] is None
    assert s["order"]["order_id"] == order["order_id"]
    assert web.get("/merchant/orders/ord_missing/status").status_code == 404


def test_small_purchase_is_allowed_and_goes_to_checkout(web):
    order = order_for(web, cheap=True)
    assert order["total_minor"] < 5000
    r = web.post(f"/merchant/orders/{order['order_id']}/checkout")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "EXECUTING" and body["checkout_url"].startswith(PUBLIC + "/sim/checkout/")
    assert body["authorization"]["provider_label"] == "Simulation (not PayPal)"
    assert body["authorization"]["agent_id"] == "ag_storefront"
    assert_no_secrets(body)
    # idempotent: same request, no second authorization
    again = web.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    assert again["request_id"] == body["request_id"]

    # buyer completes the simulated checkout; status page shows capture with a reference
    done = web.post(body["checkout_url"].replace(PUBLIC, "") + "/approve", follow_redirects=True)
    assert "Payment complete" in done.text
    s = web.get(f"/merchant/orders/{order['order_id']}/status").json()
    assert s["payment_status"] == "CAPTURED"
    assert s["authorization"]["state"] == "EXECUTED" and s["authorization"]["capture_id"]
    assert s["authorization"]["checkout_url"] is None and s["authorization"]["terminal"] is True
    assert s["authorization"]["grant"] == {"issued": True, "consumed": True}
    assert_no_secrets(s)
    assert web.post(f"/merchant/orders/{order['order_id']}/checkout").status_code == 409  # already paid


def test_approval_required_waits_for_admin_then_continues(web):
    order = order_for(web)  # >= $70 needs approval (user threshold $50)
    r = web.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    assert r["state"] == "PENDING_APPROVAL" and r["approval_pending"] and r["checkout_url"] is None
    assert r["reason_codes"]
    # polling the checkout endpoint changes nothing while the human has not approved
    assert web.post(f"/merchant/orders/{order['order_id']}/checkout").json()["request_id"] == r["request_id"]
    st = web.get(f"/merchant/orders/{order['order_id']}/status").json()
    assert st["authorization"]["state"] == "PENDING_APPROVAL" and st["authorization"]["approval_pending"]
    assert_no_secrets(st)

    detail = web.get(f"/admin/authorizations/{r['request_id']}", headers=ADMIN_H).json()
    assert web.post(f"/admin/authorizations/{r['request_id']}/approve", headers=ADMIN_H,
                    json={"request_hash": detail["request_hash"]}).status_code == 200

    cont = web.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    assert cont["state"] == "EXECUTING" and cont["checkout_url"]
    st = web.get(f"/merchant/orders/{order['order_id']}/status").json()
    assert st["authorization"]["decided_by"] == "admin:dashboard"
    assert st["authorization"]["grant"] == {"issued": True, "consumed": True}


def test_denied_merchant_shows_reason_codes_and_does_not_loop(web):
    order = order_for(web, merchant="m_quickdeals", cheap=True)
    r = web.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    assert r["state"] == "DENIED" and "MERCHANT_NOT_APPROVED" in r["reason_codes"]
    assert r["authorization"]["decision"] == "DENY"
    # polling again does not create new requests
    web.post(f"/merchant/orders/{order['order_id']}/checkout")
    created = web.get("/admin/audit", params={"event_type": "REQUEST_CREATED"}, headers=ADMIN_H).json()["events"]
    assert len(created) == 1
    # an explicit retry starts a new attempt
    web.post(f"/merchant/orders/{order['order_id']}/checkout", json={"retry": True})
    created = web.get("/admin/audit", params={"event_type": "REQUEST_CREATED"}, headers=ADMIN_H).json()["events"]
    assert len(created) == 2


def test_agent_orders_cannot_be_paid_from_the_website(web):
    order = order_for(web, buyer="ag_shopping")
    r = web.post(f"/merchant/orders/{order['order_id']}/checkout")
    assert r.status_code == 403 and r.json()["error"]["code"] == "AGENT_ORDER"


def test_agent_in_flight_order_is_not_hijacked_by_the_website(web):
    order = order_for(web, buyer="somebody")
    body = {"merchant_id": order["merchant_id"], "order_id": order["order_id"],
            "amount_minor": order["total_minor"], "currency": "USD"}
    h = {"Authorization": f"Bearer {web.keys['ag_shopping']}"}
    assert web.post("/v1/authorizations", headers=h, json=body).status_code == 201
    r = web.post(f"/merchant/orders/{order['order_id']}/checkout")
    assert r.status_code == 403 and r.json()["error"]["code"] == "AGENT_ORDER"


def test_storefront_cannot_approve(web):
    order = order_for(web)
    r = web.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    # No storefront/merchant route approves, and agent identity cannot reach admin routes.
    assert web.post(f"/admin/authorizations/{r['request_id']}/approve", json={"request_hash": "0" * 64}).status_code == 401
    openapi = web.get("/openapi.json").json()["paths"]
    assert not [p for p in openapi if p.startswith("/merchant") and "approve" in p]


def test_status_response_never_contains_secret_values(web):
    order = order_for(web, cheap=True)
    checkout = web.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    text = json.dumps(web.get(f"/merchant/orders/{order['order_id']}/status").json())
    for secret in list(web.keys.values()) + [ADMIN]:
        assert secret not in text
    assert "pact_gt" not in text and "request_hash" not in text
    assert checkout["checkout_url"] in text  # the buyer link is meant to be shown
