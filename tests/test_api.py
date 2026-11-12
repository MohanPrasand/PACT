"""#12 REST API: auth boundaries, error shape, full purchase over HTTP incl. simulated checkout redirects."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.seed import seed_catalogue
from pact_core.config import Settings
from pact_core.seed import seed_agents, seed_policies
from pact_server.app import create_app

ADMIN = "test-admin-token-0123456789abcdef"
PUBLIC = "http://testserver"


@pytest.fixture
def api(db, clock):
    settings = Settings(_env_file=None, PACT_ADMIN_TOKEN=ADMIN, PACT_PUBLIC_URL=PUBLIC,
                        PACT_PAYMENT_PROVIDER="simulation", PACT_DATABASE_URL="sqlite://")
    sim = SimulationAdapter(PUBLIC)
    app = create_app(settings, adapters={"simulation": sim}, clock=clock, start_sweeper=False, db=db)
    seed_catalogue(db)
    seed_policies(db)
    keys = seed_agents(db)
    client = TestClient(app)
    client.keys, client.sim = keys, sim
    return client


def agent_h(api, agent="ag_shopping"):
    return {"Authorization": f"Bearer {api.keys[agent]}"}


ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}


def keyboard_order(api):
    p = api.get("/merchant/products", params={"q": "keyboard", "max_price": "100.00", "sort": "rating"}).json()["products"]
    best = next(x for x in p if x["merchant_id"] == "m_techmart")
    r = api.post("/merchant/orders", json={"items": [{"product_id": best["product_id"], "quantity": 1}]})
    assert r.status_code == 201
    return r.json()


def auth_body(order):
    return {"merchant_id": order["merchant_id"], "order_id": order["order_id"], "amount_minor": order["total_minor"],
            "currency": order["currency"], "description": "keyboard"}


# ---------------------------------------------------------------- auth boundaries

def test_health_and_docs(api):
    assert api.get("/health").json() == {"status": "ok", "provider": "simulation", "currency": "USD"}
    assert api.get("/openapi.json").status_code == 200


def test_agent_routes_require_valid_key(api):
    assert api.get("/v1/budget").status_code == 401
    r = api.get("/v1/budget", headers={"Authorization": "Bearer pact_ak_nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "AGENT_AUTH_INVALID"
    assert api.get("/v1/me", headers=agent_h(api)).json()["agent_id"] == "ag_shopping"


@pytest.mark.parametrize("method,path", [
    ("get", "/admin/overview"), ("get", "/admin/authorizations"), ("post", "/admin/authorizations/x/approve"),
    ("post", "/admin/authorizations/x/deny"), ("put", "/admin/policies/org:default"), ("get", "/admin/audit"),
])
def test_agent_key_cannot_use_admin_routes(api, method, path):
    """An agent cannot approve its own payment (or touch policy) by calling the API directly."""
    body = None if method == "get" else {}
    r = api.request(method.upper(), path, headers=agent_h(api), json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ADMIN_AUTH_INVALID"
    assert api.request(method.upper(), path, json=body).status_code == 401


def test_admin_token_never_accepted_as_agent(api):
    assert api.get("/v1/budget", headers=ADMIN_H).status_code == 401


def test_error_shape_and_validation(api):
    r = api.post("/v1/authorizations", headers=agent_h(api), json={"merchant_id": "m_techmart"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_REQUEST"
    r = api.post("/merchant/orders", json={"items": [{"product_id": "p", "quantity": 1, "price_minor": 1}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST"
    r = api.get("/v1/authorizations/auth_missing", headers=agent_h(api))
    assert r.status_code == 404 and r.json()["error"]["code"] == "REQUEST_NOT_FOUND"


# ---------------------------------------------------------------- the acceptance flow over HTTP

def test_full_purchase_over_http_then_replay_rejected(api):
    order = keyboard_order(api)
    r = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(order))
    assert r.status_code == 201
    req = r.json()
    assert req["state"] == "PENDING_APPROVAL" and req["decision"]["reason_codes"] == ["USER_APPROVAL_THRESHOLD"]

    pending = api.get("/admin/authorizations", params={"state": "PENDING_APPROVAL"}, headers=ADMIN_H).json()
    assert [a["request_id"] for a in pending["authorizations"]] == [req["request_id"]]
    detail = api.get(f"/admin/authorizations/{req['request_id']}", headers=ADMIN_H).json()
    assert detail["merchant_name"] == "TechMart" and detail["order_snapshot"]["items"]

    a = api.post(f"/admin/authorizations/{req['request_id']}/approve", headers=ADMIN_H,
                 json={"request_hash": detail["request_hash"], "note": "ok"})
    assert a.status_code == 200 and a.json()["state"] == "APPROVED"

    s = api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api)).json()
    token = s["grant_token"]
    assert "grant_token" not in api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api)).json()

    exec_body = {"grant_token": token, "merchant_id": order["merchant_id"], "order_id": order["order_id"],
                 "amount_minor": order["total_minor"], "currency": "USD"}
    e = api.post(f"/v1/authorizations/{req['request_id']}/execute", headers=agent_h(api), json=exec_body).json()
    assert e["state"] == "EXECUTING"
    checkout = e["execution"]["approval_url"].replace(PUBLIC, "")
    page = api.get(checkout)
    assert page.status_code == 200 and "SIMULATED CHECKOUT" in page.text and "not PayPal" in page.text

    done = api.post(f"{checkout}/approve", follow_redirects=True)  # -> /payments/return -> capture
    assert done.status_code == 200 and "Payment complete" in done.text and "Simulation (not PayPal)" in done.text

    final = api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api)).json()
    assert final["state"] == "EXECUTED" and final["execution"]["capture_id"]
    assert api.get(f"/merchant/orders/{order['order_id']}").json()["payment_status"] == "CAPTURED"

    replay = api.post(f"/v1/authorizations/{req['request_id']}/execute", headers=agent_h(api), json=exec_body)
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "GRANT_ALREADY_USED"
    events = api.get("/admin/audit", params={"event_type": "REPLAY_REJECTED"}, headers=ADMIN_H).json()["events"]
    assert len(events) == 1
    assert api.get("/admin/audit/verify", headers=ADMIN_H).json()["valid"] is True


def test_cancel_redirect(api):
    order = keyboard_order(api)
    req = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(order)).json()
    api.post(f"/admin/authorizations/{req['request_id']}/approve", headers=ADMIN_H, json={"request_hash": req["request_hash"]})
    token = api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api)).json()["grant_token"]
    e = api.post(f"/v1/authorizations/{req['request_id']}/execute", headers=agent_h(api), json={
        "grant_token": token, "merchant_id": order["merchant_id"], "order_id": order["order_id"],
        "amount_minor": order["total_minor"], "currency": "USD"}).json()
    page = api.post(e["execution"]["approval_url"].replace(PUBLIC, "") + "/cancel", follow_redirects=True)
    assert "Payment not completed" in page.text and "BUYER_CANCELLED" in page.text
    assert api.get(f"/merchant/orders/{order['order_id']}").json()["payment_status"] == "CANCELLED"


def test_unknown_return_token_page(api):
    r = api.get("/payments/return", params={"token": "SIM-NOPE"})
    assert r.status_code == 404


def test_deny_and_revoke_routes(api):
    order = keyboard_order(api)
    req = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(order)).json()
    d = api.post(f"/admin/authorizations/{req['request_id']}/deny", headers=ADMIN_H, json={"reason": "no"})
    assert d.json()["state"] == "DENIED"
    order2 = keyboard_order(api)
    req2 = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(order2)).json()
    assert api.post(f"/v1/authorizations/{req2['request_id']}/revoke", headers=agent_h(api)).json()["state"] == "REVOKED"


def test_policy_update_via_api(api):
    pol = {p["id"]: p for p in api.get("/admin/policies", headers=ADMIN_H).json()["policies"]}["user:u_demo"]
    bad = api.put("/admin/policies/user:u_demo", headers=ADMIN_H,
                  json={"rules": {**pol["rules"], "approval_threshold_minor": -1}, "expected_version": pol["version"]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "POLICY_INVALID"
    ok = api.put("/admin/policies/user:u_demo", headers=ADMIN_H,
                 json={"rules": {**pol["rules"], "approval_threshold_minor": 10000}, "expected_version": pol["version"]})
    assert ok.status_code == 200 and ok.json()["version"] == pol["version"] + 1
    stale = api.put("/admin/policies/user:u_demo", headers=ADMIN_H,
                    json={"rules": pol["rules"], "expected_version": pol["version"]})
    assert stale.status_code == 409


def test_overview_agents_budget(api):
    ov = api.get("/admin/overview", headers=ADMIN_H).json()
    assert {b["scope"] for b in ov["budgets"]} >= {"org:global", "user:u_demo", "agent:ag_shopping"}
    assert {a["agent_id"] for a in ov["agents"]} == {"ag_shopping", "ag_research", "ag_procurement"}
    assert all("api_key_hash" not in a for a in ov["agents"])
    b = api.get("/v1/budget", headers=agent_h(api)).json()
    assert {s["scope"] for s in b["scopes"]} == {"org:global", "user:u_demo", "agent:ag_shopping"}


def test_cart_endpoints(api):
    p = api.get("/merchant/products", params={"q": "mouse"}).json()["products"][0]
    api.post("/merchant/carts/ag_shopping/items", json={"product_id": p["product_id"], "quantity": 2})
    cart = api.get("/merchant/carts/ag_shopping").json()
    assert cart["total_minor"] == 2 * p["price_minor"]
    order = api.post("/merchant/carts/ag_shopping/checkout", json={}).json()
    assert order["total_minor"] == 2 * p["price_minor"] and order["buyer_ref"] == "ag_shopping"
    assert api.get("/merchant/carts/ag_shopping").json()["items"] == []
    assert api.get("/merchant/transactions", params={"buyer_ref": "ag_shopping"}).json()["orders"]


def test_admin_disabled_without_token(db, clock):
    settings = Settings(_env_file=None, PACT_DATABASE_URL="sqlite://")
    client = TestClient(create_app(settings, adapters={"simulation": SimulationAdapter(PUBLIC)}, clock=clock,
                                   start_sweeper=False, db=db))
    r = client.get("/admin/overview", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "ADMIN_DISABLED"
