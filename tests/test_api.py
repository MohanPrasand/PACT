"""#12 REST API: auth boundaries, error shape, full purchase over HTTP incl. simulated checkout redirects."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.seed import seed_catalogue
from pact_core.config import Settings
from pact_core.errors import NotFound
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


def executing_payment(api):
    """Drive a purchase to EXECUTING (checkout open at the provider). Returns (order, view)."""
    order = keyboard_order(api)
    req = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(order)).json()
    api.post(f"/admin/authorizations/{req['request_id']}/approve", headers=ADMIN_H, json={"request_hash": req["request_hash"]})
    token = api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api)).json()["grant_token"]
    e = api.post(f"/v1/authorizations/{req['request_id']}/execute", headers=agent_h(api), json={
        "grant_token": token, "merchant_id": order["merchant_id"], "order_id": order["order_id"],
        "amount_minor": order["total_minor"], "currency": "USD"}).json()
    assert e["state"] == "EXECUTING"
    return order, e


def contract_view(view, state="EXECUTING", **execution):
    """A real view reshaped to the frozen checkout contract (docs/plans/paypal-gateway-fixes.md)."""
    return {**view, "state": state, "execution": {**view["execution"], **execution}}


class FakeCore:
    """Replaces the two PactCore methods whose behaviour is changing, recording every call."""

    def __init__(self, api, monkeypatch, *, cancel=None, abandon=None):
        self.calls: list[tuple[str, str]] = []
        core = api.app.state.core
        monkeypatch.setattr(core, "handle_provider_cancel", self._method("handle_provider_cancel", cancel), raising=False)
        monkeypatch.setattr(core, "buyer_abandon", self._method("buyer_abandon", abandon), raising=False)

    def _method(self, name, result):
        def call(provider_order_id):
            self.calls.append((name, provider_order_id))
            if isinstance(result, Exception):
                raise result
            assert result is not None, f"{name} must not be called"
            return result
        return call


def test_cancel_redirect(api, monkeypatch):
    """Simulated cancel -> /payments/cancel is non-terminal: the page offers "Try again" and an explicit cancel."""
    _order, e = executing_payment(api)
    oid, url = e["execution"]["provider_order_id"], e["execution"]["approval_url"]
    fake = FakeCore(api, monkeypatch, cancel=contract_view(e, provider_status="BUYER_CANCELLED"))

    page = api.post(url.replace(PUBLIC, "") + "/cancel", follow_redirects=True)
    assert page.status_code == 200 and page.url.path == "/payments/cancel"
    assert "Checkout cancelled" in page.text and "Nothing was charged" in page.text and "still open" in page.text
    assert "Payment not completed" not in page.text
    assert f'<a class=\'btn\' href="{url}">Try again</a>' in page.text
    assert "action='/payments/abandon'" in page.text and "method='post'" in page.text
    assert f"<input type='hidden' name='token' value=\"{oid}\">" in page.text
    assert "Cancel this payment" in page.text
    assert "Sandbox" not in page.text  # the PayPal login hint is for the PayPal provider only
    assert fake.calls == [("handle_provider_cancel", oid)]  # a GET never abandons


def test_sim_cancel_end_to_end_real_core(api):
    """No fakes (#40): the cancel link leaves the payment open, and the same checkout can still be paid."""
    order, e = executing_payment(api)
    rid, checkout = e["request_id"], e["execution"]["approval_url"].replace(PUBLIC, "")
    page = api.post(checkout + "/cancel", follow_redirects=True)
    assert page.status_code == 200 and page.url.path == "/payments/cancel"
    assert "Checkout cancelled" in page.text and "Nothing was charged" in page.text
    assert "Try again" in page.text and "/payments/abandon" in page.text

    st = api.get(f"/v1/authorizations/{rid}", headers=agent_h(api)).json()
    assert st["state"] == "EXECUTING" and st["execution"]["provider_status"] == "BUYER_CANCELLED"

    done = api.post(checkout + "/approve", follow_redirects=True)  # "Try again", then pay
    assert "Payment complete" in done.text
    assert api.get(f"/v1/authorizations/{rid}", headers=agent_h(api)).json()["state"] == "EXECUTED"
    assert api.get(f"/merchant/orders/{order['order_id']}").json()["payment_status"] == "CAPTURED"


def test_explicit_buyer_cancel_end_to_end_real_core(api):
    """No fakes (#40): only the explicit POST ends the payment and releases the reservation."""
    order, e = executing_payment(api)
    rid, poid = e["request_id"], e["execution"]["provider_order_id"]
    api.post(e["execution"]["approval_url"].replace(PUBLIC, "") + "/cancel", follow_redirects=True)
    page = api.post("/payments/abandon", data={"token": poid})
    assert page.status_code == 200 and "Payment not completed" in page.text and "BUYER_CANCELLED" in page.text
    st = api.get(f"/v1/authorizations/{rid}", headers=agent_h(api)).json()
    assert st["state"] == "FAILED" and st["execution"]["failure_reason"] == "BUYER_CANCELLED"
    assert api.get(f"/merchant/orders/{order['order_id']}").json()["payment_status"] == "CANCELLED"
    again = api.post("/payments/abandon", data={"token": poid})  # double submit is harmless
    assert again.status_code == 200 and "BUYER_CANCELLED" in again.text


def test_cancel_page_escapes_approval_url_and_shows_paypal_hint(api, monkeypatch):
    _order, e = executing_payment(api)
    url = "https://www.sandbox.paypal.com/checkoutnow?token=5O1&x=\"><script>alert(1)</script>"
    FakeCore(api, monkeypatch, cancel=contract_view(
        e, provider_status="BUYER_CANCELLED", approval_url=url, provider="paypal",
        provider_label="PayPal <Sandbox>", provider_order_id="5O1\"'><b>"))
    page = api.get("/payments/cancel", params={"token": "5O1"})
    assert page.status_code == 200 and "Checkout cancelled" in page.text
    assert ('href="https://www.sandbox.paypal.com/checkoutnow?token=5O1&amp;x=&quot;&gt;&lt;script&gt;'
            'alert(1)&lt;/script&gt;">Try again</a>') in page.text
    assert "<script>" not in page.text and "<b>" not in page.text
    assert "value=\"5O1&quot;&#x27;&gt;&lt;b&gt;\"" in page.text
    assert "PayPal &lt;Sandbox&gt;" in page.text
    assert "personal" in page.text and "docs/paypal-sandbox-setup.md" in page.text
    assert "start a new purchase" not in page.text  # the payment is still open: retry in place


@pytest.mark.parametrize("hostile", ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                                     "//evil.example/x", " https://evil.example", "", None])
@pytest.mark.parametrize("status", ["BUYER_CANCELLED", "INSTRUMENT_DECLINED"])
def test_hostile_approval_url_is_not_linked(api, monkeypatch, status, hostile):
    _order, e = executing_payment(api)
    FakeCore(api, monkeypatch, cancel=contract_view(e, provider_status=status, approval_url=hostile))
    page = api.get("/payments/cancel", params={"token": e["execution"]["provider_order_id"]})
    assert page.status_code == 200
    assert "<a " not in page.text and "href" not in page.text
    assert "javascript:" not in page.text and "evil.example" not in page.text and "<script>" not in page.text
    if status == "BUYER_CANCELLED":
        assert "Cancel this payment" in page.text  # the explicit cancel stays available


def test_cancel_query_token_is_never_reflected(api, monkeypatch):
    _order, e = executing_payment(api)
    FakeCore(api, monkeypatch, cancel=contract_view(e, provider_status="BUYER_CANCELLED"))
    page = api.get("/payments/cancel", params={"token": "<script>zzq</script>"})
    assert page.status_code == 200 and "zzq" not in page.text


def test_cancel_after_capture_falls_through_to_result_page(api, monkeypatch):
    _order, e = executing_payment(api)
    fake = FakeCore(api, monkeypatch, cancel=contract_view(e, state="EXECUTED", provider_status="COMPLETED",
                                                           capture_id="CAP-1"))
    page = api.get("/payments/cancel", params={"token": e["execution"]["provider_order_id"]})
    assert "Payment complete" in page.text and "CAP-1" in page.text
    assert "Cancel this payment" not in page.text and "Try again" not in page.text
    assert [c[0] for c in fake.calls] == ["handle_provider_cancel"]


def test_abandon_calls_buyer_abandon_and_renders_failed_page(api, monkeypatch):
    _order, e = executing_payment(api)
    oid = e["execution"]["provider_order_id"]
    fake = FakeCore(api, monkeypatch, abandon=contract_view(
        e, state="FAILED", provider_status="BUYER_CANCELLED", failure_reason="BUYER_CANCELLED", provider="paypal"))
    page = api.post("/payments/abandon", data={"token": oid})
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Payment not completed" in page.text and "BUYER_CANCELLED" in page.text
    assert "Nothing was charged" in page.text
    assert "Try again" not in page.text and "Cancel this payment" not in page.text and "<a " not in page.text
    assert "docs/paypal-sandbox-setup.md" in page.text and "start a new purchase" in page.text
    assert fake.calls == [("buyer_abandon", oid)]


def test_abandon_already_captured_renders_complete(api, monkeypatch):
    _order, e = executing_payment(api)
    FakeCore(api, monkeypatch, abandon=contract_view(e, state="EXECUTED", provider_status="COMPLETED",
                                                     capture_id="CAP-9"))
    page = api.post("/payments/abandon", data={"token": e["execution"]["provider_order_id"]})
    assert page.status_code == 200 and "Payment complete" in page.text and "CAP-9" in page.text


def test_abandon_unknown_token_is_404_page(api, monkeypatch):
    fake = FakeCore(api, monkeypatch, abandon=NotFound("REQUEST_NOT_FOUND", "No payment for <order>"))
    page = api.post("/payments/abandon", data={"token": "SIM-NOPE"})
    assert page.status_code == 404 and "Payment not found" in page.text
    assert "No payment for &lt;order&gt;" in page.text and "<order>" not in page.text
    assert fake.calls == [("buyer_abandon", "SIM-NOPE")]


_FORM = {"content-type": "application/x-www-form-urlencoded"}


@pytest.mark.parametrize("kwargs", [
    {"data": {"token": "x" * 65}}, {"data": {}}, {"data": {"token": ""}}, {"data": {"other": "SIM-1"}},
    {"content": b"token=a&token=b", "headers": _FORM},
    {"content": b"token=\xff\xfe", "headers": _FORM},
    {"content": b"token=a&pad=" + b"x" * 2000, "headers": _FORM},
    {"json": {"token": "SIM-1"}},
], ids=["too-long", "empty-body", "blank", "missing", "duplicate", "not-utf8", "oversized-body", "json"])
def test_abandon_invalid_token_is_rejected_without_core_call(api, monkeypatch, kwargs):
    fake = FakeCore(api, monkeypatch)
    r = api.post("/payments/abandon", **kwargs)
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_REQUEST"
    assert fake.calls == []


def test_abandon_accepts_64_char_token_and_get_is_not_allowed(api, monkeypatch):
    _order, e = executing_payment(api)
    fake = FakeCore(api, monkeypatch, abandon=contract_view(e, state="FAILED", failure_reason="BUYER_CANCELLED"))
    assert api.post("/payments/abandon", data={"token": "x" * 64}).status_code == 200
    assert fake.calls == [("buyer_abandon", "x" * 64)]
    assert api.get("/payments/abandon", params={"token": "x"}).status_code in (404, 405)
    assert len(fake.calls) == 1


def test_cancel_query_token_validation(api, monkeypatch):
    fake = FakeCore(api, monkeypatch)
    assert api.get("/payments/cancel", params={"token": "x" * 65}).status_code == 422
    assert api.get("/payments/cancel").status_code == 422
    assert fake.calls == []


def test_declined_page_links_back_to_checkout(api, monkeypatch):
    _order, e = executing_payment(api)
    url = e["execution"]["approval_url"]
    monkeypatch.setattr(api.app.state.core, "handle_provider_return",
                        lambda _t: contract_view(e, provider_status="INSTRUMENT_DECLINED"))
    page = api.get("/payments/return", params={"token": e["execution"]["provider_order_id"]})
    assert page.status_code == 200 and "Payment method declined" in page.text
    assert "PayPal declined the payment method" in page.text and "Nothing was charged" in page.text
    assert f'href="{url}">Choose another payment method</a>' in page.text
    assert "Cancel this payment" not in page.text and "Payment not completed" not in page.text


@pytest.mark.parametrize("reason,expected", [
    ("CAPTURE_REJECTED:COMPLIANCE_VIOLATION", ["PayPal refused the payment and no money moved",
                                               "<code>COMPLIANCE_VIOLATION</code>"]),
    ("CAPTURE_REJECTED:<img src=x>", ["PayPal refused the payment", "<code>&lt;img src=x&gt;</code>"]),
    ("CHECKOUT_TIMEOUT", ["not completed in time", "Nothing was charged"]),
    ("PROVIDER_ORDER_NOT_FOUND", ["PayPal order expired", "Nothing was charged"]),
])
def test_failure_reason_pages(api, monkeypatch, reason, expected):
    _order, e = executing_payment(api)
    view = contract_view(e, state="FAILED", failure_reason=reason, provider="paypal", provider_label="PayPal Sandbox")
    monkeypatch.setattr(api.app.state.core, "handle_provider_return", lambda _t: view)
    page = api.get("/payments/return", params={"token": "5O1"})
    assert page.status_code == 200 and "Payment not completed" in page.text
    for text in expected:
        assert text in page.text
    assert "<img" not in page.text and "<a " not in page.text
    assert "docs/paypal-sandbox-setup.md" not in page.text  # the login hint is for cancelled checkouts only


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
