"""#15 acceptance (spec §14.5, §19): the whole PACT flow through the REST API with the PayPal adapter on a mock transport.

No real PayPal, no .env, no real database. A stateful fake plays PayPal Orders v2: it echoes what PACT creates
(reference id, amount) and lets the "buyer" approve before capture, like the real Sandbox would.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from adapters.paypal.adapter import PayPalAdapter
from merchant_lab.seed import seed_catalogue
from pact_core.config import Settings
from pact_core.seed import seed_agents, seed_policies
from pact_server.app import create_app

ADMIN = "test-admin-token-0123456789abcdef"
PUBLIC = "http://testserver"
ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}


class FakePayPalOrders:
    """Minimal stateful PayPal Orders v2 (create -> buyer approval -> capture)."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def approve_as_buyer(self, order_id: str) -> None:
        self.orders[order_id]["buyer_approved"] = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21AA-fake-token", "token_type": "Bearer",
                                             "expires_in": 32400})
        if request.method == "POST" and path == "/v2/checkout/orders":
            body = json.loads(request.content)
            unit = body["purchase_units"][0]
            oid = f"5O190127TN3647{len(self.orders):02d}T"
            ctx = body["payment_source"]["paypal"]["experience_context"]
            self.orders[oid] = {"unit": unit, "return_url": ctx["return_url"], "buyer_approved": False,
                                "captured": False}
            return httpx.Response(201, json={
                "id": oid, "status": "PAYER_ACTION_REQUIRED", "purchase_units": [unit],
                "links": [{"href": f"https://www.sandbox.paypal.com/checkoutnow?token={oid}",
                           "rel": "payer-action", "method": "GET"}]})
        m = re.fullmatch(r"/v2/checkout/orders/([A-Z0-9]+)(/capture)?", path)
        if m and m.group(1) in self.orders:
            o = self.orders[m.group(1)]
            if m.group(2):
                if not o["buyer_approved"]:
                    return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY",
                                                     "details": [{"issue": "ORDER_NOT_APPROVED"}]})
                o["captured"] = True
            unit = dict(o["unit"])
            if o["captured"]:
                unit["payments"] = {"captures": [{"id": "CAP" + m.group(1)[-8:], "status": "COMPLETED",
                                                  "amount": unit["amount"], "final_capture": True}]}
                status = "COMPLETED"
            else:
                status = "APPROVED" if o["buyer_approved"] else "PAYER_ACTION_REQUIRED"
            return httpx.Response(200, json={"id": m.group(1), "status": status, "purchase_units": [unit]})
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "details": [{"issue": "INVALID_RESOURCE_ID"}]})


@pytest.fixture
def stack(db, clock):
    fake = FakePayPalOrders()
    adapter = PayPalAdapter("test-client-id", "test-client-secret", transport=httpx.MockTransport(fake))
    settings = Settings(_env_file=None, PACT_ADMIN_TOKEN=ADMIN, PACT_PUBLIC_URL=PUBLIC,
                        PACT_PAYMENT_PROVIDER="paypal", PAYPAL_CLIENT_ID="test-client-id",
                        PAYPAL_CLIENT_SECRET="test-client-secret", PACT_DATABASE_URL="sqlite://")
    app = create_app(settings, adapters={"paypal": adapter}, clock=clock, start_sweeper=False, db=db)
    seed_catalogue(db)
    seed_policies(db)
    keys = seed_agents(db)
    client = TestClient(app)
    client.keys, client.fake = keys, fake
    yield client
    adapter.close()


def agent_h(c, agent="ag_shopping"):
    return {"Authorization": f"Bearer {c.keys[agent]}"}


def order_of(c, product_id: str, buyer: str, quantity: int = 1) -> dict:
    r = c.post("/merchant/orders", json={"items": [{"product_id": product_id, "quantity": quantity}],
                                         "buyer_ref": buyer})
    assert r.status_code == 201, r.text
    return r.json()


def auth_body(order: dict) -> dict:
    return {"merchant_id": order["merchant_id"], "order_id": order["order_id"], "amount_minor": order["total_minor"],
            "currency": order["currency"], "description": "e2e"}


def scope(c, key: str, agent="ag_shopping") -> dict:
    return next(s for s in c.get("/v1/budget", headers=agent_h(c, agent)).json()["scopes"] if s["scope"] == key)


def buyer_pays(c, approval_url: str):
    """Simulated buyer: approve at PayPal, then PayPal redirects the browser to /payments/return?token=<order id>."""
    oid = approval_url.split("token=")[1]
    c.fake.approve_as_buyer(oid)
    return c.get("/payments/return", params={"token": oid})


# ------------------------------------------------------------------------------------------------ 1. agent purchase

def test_agent_purchase_end_to_end_then_replay_rejected(stack):
    c = stack
    # 1. shopping agent searches and creates an order above the approval threshold ($50)
    found = c.get("/merchant/products", params={"q": "keyboard", "max_price": "100.00", "sort": "rating"}).json()["products"]
    best = next(p for p in found if p["merchant_id"] == "m_techmart")
    assert best["price_minor"] == 7900
    order = order_of(c, best["product_id"], "ag_shopping")
    assert order["total_minor"] > 5000

    # 2. request -> PENDING_APPROVAL, budget reserved, nothing at PayPal yet
    req = c.post("/v1/authorizations", headers=agent_h(c), json=auth_body(order)).json()
    rid = req["request_id"]
    assert req["state"] == "PENDING_APPROVAL" and "USER_APPROVAL_THRESHOLD" in req["decision"]["reason_codes"]
    assert scope(c, "org:global")["reserved_minor"] == 7900
    assert not [x for x in c.fake.calls if x[1].startswith("/v2/")]

    # 3. admin approves exactly what the dashboard displays (hash bound)
    detail = c.get(f"/admin/authorizations/{rid}", headers=ADMIN_H).json()
    assert detail["state"] == "PENDING_APPROVAL" and detail["order_snapshot"]["items"]
    wrong = c.post(f"/admin/authorizations/{rid}/approve", headers=ADMIN_H, json={"request_hash": "0" * 64})
    assert wrong.status_code >= 400
    assert c.post(f"/admin/authorizations/{rid}/approve", headers=ADMIN_H,
                  json={"request_hash": detail["request_hash"]}).json()["state"] == "APPROVED"

    # 4. agent fetches status: the grant token is shown exactly once
    st = c.get(f"/v1/authorizations/{rid}", headers=agent_h(c)).json()
    token = st["grant_token"]
    assert token.startswith("pact_gr")
    assert "grant_token" not in c.get(f"/v1/authorizations/{rid}", headers=agent_h(c)).json()

    # 5. execute -> PayPal approval URL -> buyer approval -> /payments/return -> capture
    exec_body = {"grant_token": token, "merchant_id": order["merchant_id"], "order_id": order["order_id"],
                 "amount_minor": order["total_minor"], "currency": "USD"}
    ex = c.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(c), json=exec_body).json()
    assert ex["state"] == "EXECUTING"
    url = ex["execution"]["approval_url"]
    assert url.startswith("https://www.sandbox.paypal.com/checkoutnow?token=")
    page = buyer_pays(c, url)
    assert page.status_code == 200 and "Payment complete" in page.text

    # 6. EXECUTED, order CAPTURED, budget settled, audit chain valid
    final = c.get(f"/v1/authorizations/{rid}", headers=agent_h(c)).json()
    assert final["state"] == "EXECUTED" and final["execution"]["capture_id"].startswith("CAP")
    assert c.get(f"/merchant/orders/{order['order_id']}").json()["payment_status"] == "CAPTURED"
    org = scope(c, "org:global")
    assert (org["reserved_minor"], org["settled_minor"]) == (0, 7900)
    assert c.get("/admin/audit/verify", headers=ADMIN_H).json()["valid"] is True

    # 7. replay of the consumed grant is rejected and audited
    replay = c.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(c), json=exec_body)
    assert replay.status_code == 409 and replay.json()["error"]["code"] == "GRANT_ALREADY_USED"
    events = c.get("/admin/audit", params={"event_type": "REPLAY_REJECTED", "request_id": rid},
                   headers=ADMIN_H).json()["events"]
    assert len(events) == 1
    assert scope(c, "org:global")["settled_minor"] == 7900  # no second charge
    assert len([x for x in c.fake.calls if x[0] == "POST" and x[1].endswith("/capture")]) == 1
    assert c.get("/admin/audit/verify", headers=ADMIN_H).json()["valid"] is True
    # secrets never leak into the audit trail
    blob = json.dumps(c.get("/admin/audit", params={"limit": 500}, headers=ADMIN_H).json())
    assert token not in blob and ADMIN not in blob and "test-client-secret" not in blob


# ------------------------------------------------------------------------------------------------ 2. shared budget

def test_multi_agent_shared_budget_denies_last_request_within_own_cap(stack):
    """Spec §2: each agent respects its own cap, yet the organisation's daily budget ($500) is shared."""
    c = stack
    plan = [  # (agent, product, quantity) -> amounts in cents
        ("ag_shopping", "prd_kb_titan_rgb", 1),       # 139.00
        ("ag_shopping", "prd_kb_forge_75", 1),        #  64.00   shopping total 203 (cap 250)
        ("ag_procurement", "prd_monitor_27_qhd", 1),  # 189.00
        ("ag_procurement", "prd_hub_usbc_7", 4),      #  96.00   procurement total 285 (cap 300)
    ]
    for agent, product, qty in plan:
        o = order_of(c, product, agent, qty)
        r = c.post("/v1/authorizations", headers=agent_h(c, agent), json=auth_body(o))
        assert r.status_code == 201
        assert r.json()["state"] == "PENDING_APPROVAL", r.json()  # reserves the shared budget
    assert scope(c, "org:global")["reserved_minor"] == 13900 + 6400 + 18900 + 9600  # 488.00 of 500.00
    assert scope(c, "agent:ag_research", "ag_research")["available_minor"] > 4500  # own cap would allow it

    o = order_of(c, "prd_ds_retail_prices", "ag_research")  # 45.00 <= research tx/daily limits, but > $12 left
    r = c.post("/v1/authorizations", headers=agent_h(c, "ag_research"), json=auth_body(o)).json()
    assert r["state"] == "DENIED"
    assert "BUDGET_EXCEEDED" in r["decision"]["reason_codes"]
    assert "org:global" in json.dumps(r["decision"])  # denied by the global budget, not an agent rule
    assert c.get("/admin/audit/verify", headers=ADMIN_H).json()["valid"] is True


# ------------------------------------------------------------------------------------------------ 3. human web purchase

def test_human_web_purchase_goes_through_pact_the_same_way(stack):
    c = stack
    order = order_of(c, "prd_kb_atlas_tkl", "web_visitor")
    first = c.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    assert first["state"] == "PENDING_APPROVAL" and first["checkout_url"] is None
    rid = first["request_id"]
    assert scope(c, "org:global", "ag_storefront")["reserved_minor"] == 7900

    detail = c.get(f"/admin/authorizations/{rid}", headers=ADMIN_H).json()
    assert detail["agent_id"] == "ag_storefront"
    c.post(f"/admin/authorizations/{rid}/approve", headers=ADMIN_H, json={"request_hash": detail["request_hash"]})

    cont = c.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    assert cont["state"] == "EXECUTING" and cont["checkout_url"].startswith("https://www.sandbox.paypal.com/")
    assert buyer_pays(c, cont["checkout_url"]).status_code == 200

    st = c.get(f"/merchant/orders/{order['order_id']}/status").json()
    assert st["payment_status"] == "CAPTURED" and st["authorization"]["state"] == "EXECUTED"
    assert st["authorization"]["grant"] == {"issued": True, "consumed": True}
    assert scope(c, "org:global", "ag_storefront")["settled_minor"] == 7900
    assert c.post(f"/merchant/orders/{order['order_id']}/checkout").status_code == 409  # already paid
    assert c.get("/admin/audit/verify", headers=ADMIN_H).json()["valid"] is True
