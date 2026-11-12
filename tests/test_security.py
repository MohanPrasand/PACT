"""#16 security hardening: black-box checks of the spec section 12 protections through the REST API and MCP surface.

docs/SECURITY.md maps every checklist item to a test; this file covers the items the module-level suites only cover
at unit level (or not at all). Tests marked ``xfail(strict=True, reason="SEC-n: ...")`` demonstrate a confirmed,
not-yet-fixed vulnerability from docs/SECURITY.md: the fix flips them to XPASS(strict) = failure, which forces the
marker to be removed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import pact_core.core as core_module
import pact_server.app as app_module
from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.seed import seed_catalogue
from pact_core.config import Settings
from pact_core.models import Agent, Policy
from pact_core.seed import seed_agents, seed_policies
from pact_mcp.server import build_server
from pact_server.app import create_app

ADMIN = "test-admin-token-0123456789abcdef"
PAYPAL_SECRET = "paypal-client-secret-SHOULD-NEVER-LEAK-9f8e7d"
PUBLIC = "http://testserver"
ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(db, clock):
    settings = Settings(_env_file=None, PACT_ADMIN_TOKEN=ADMIN, PACT_PUBLIC_URL=PUBLIC,
                        PACT_PAYMENT_PROVIDER="simulation", PACT_DATABASE_URL="sqlite://",
                        PAYPAL_CLIENT_ID="sandbox-client-id", PAYPAL_CLIENT_SECRET=PAYPAL_SECRET)
    sim = SimulationAdapter(PUBLIC)
    app = create_app(settings, adapters={"simulation": sim}, clock=clock, start_sweeper=False, db=db)
    seed_catalogue(db)
    seed_policies(db)
    client = TestClient(app, raise_server_exceptions=False)
    client.keys, client.sim, client.db = seed_agents(db), sim, db
    return client


def agent_h(api, agent="ag_shopping"):
    return {"Authorization": f"Bearer {api.keys[agent]}"}


def make_order(api, *, buyer="ag_shopping", cheap=False):
    products = api.get("/merchant/products", params={"merchant_id": "m_techmart", "sort": "price"}).json()["products"]
    pick = products[0] if cheap else next(p for p in products if p["price_minor"] >= 7000)
    r = api.post("/merchant/orders", json={"items": [{"product_id": pick["product_id"], "quantity": 1}],
                                           "buyer_ref": buyer})
    assert r.status_code == 201
    return r.json()


def auth_body(order):
    return {"merchant_id": order["merchant_id"], "order_id": order["order_id"], "amount_minor": order["total_minor"],
            "currency": order["currency"], "description": "security test"}


def exec_body(order, token, **override):
    body = {"grant_token": token, "merchant_id": order["merchant_id"], "order_id": order["order_id"],
            "amount_minor": order["total_minor"], "currency": order["currency"]}
    body.update(override)
    return body


def approved_grant(api, order=None, agent="ag_shopping"):
    """Order, request id and (once-delivered) grant token, approved through the admin route."""
    order = order or make_order(api, buyer=agent)
    req = api.post("/v1/authorizations", headers=agent_h(api, agent), json=auth_body(order)).json()
    assert req["state"] == "PENDING_APPROVAL", req
    r = api.post(f"/admin/authorizations/{req['request_id']}/approve", headers=ADMIN_H,
                 json={"request_hash": req["request_hash"]})
    assert r.status_code == 200
    status = api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api, agent))
    return order, req, status.json()["grant_token"], status


# ===================================================================== 1. secrets never leave the server

def test_secrets_never_appear_in_responses_logs_audit_or_database(api, caplog):
    caplog.set_level(logging.DEBUG)
    bodies: list[tuple[str, str]] = []

    def seen(label, resp):
        bodies.append((label, resp.text))
        return resp

    order, req, token, delivery = approved_grant(api)
    rid = req["request_id"]
    # Failures on purpose: bad keys, bad admin token, tampering, replay, malformed body echoing the token.
    seen("bad-key", api.get("/v1/me", headers={"Authorization": f"Bearer {api.keys['ag_shopping']}x"}))
    seen("bad-admin", api.get("/admin/overview", headers={"Authorization": f"Bearer {ADMIN}x"}))
    seen("agent-on-admin", api.get("/admin/overview", headers=agent_h(api)))
    seen("tamper", api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api),
                            json=exec_body(order, token, amount_minor=order["total_minor"] - 1)))
    seen("malformed", api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api),
                               json=exec_body(order, token, amount_minor="abc", extra_field=token)))
    seen("other-agent", api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api, "ag_research"),
                                 json=exec_body(order, token)))
    ok = seen("execute", api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api),
                                  json=exec_body(order, token)))
    assert ok.json()["state"] == "EXECUTING"
    seen("replay", api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api), json=exec_body(order, token)))
    checkout = ok.json()["execution"]["approval_url"].replace(PUBLIC, "")
    seen("sim-page", api.get(checkout))
    seen("paid", api.post(f"{checkout}/approve", follow_redirects=True))
    seen("status", api.get(f"/v1/authorizations/{rid}", headers=agent_h(api)))
    seen("agent-audit", api.get("/v1/audit", headers=agent_h(api)))
    seen("budget", api.get("/v1/budget", headers=agent_h(api)))
    for path in ("/admin/overview", "/admin/authorizations", f"/admin/authorizations/{rid}", "/admin/policies",
                 "/admin/agents", "/admin/budgets", "/admin/audit", "/admin/audit/verify"):
        seen(path, api.get(path, headers=ADMIN_H))
    for path in ("/health", "/openapi.json", "/merchant/transactions", f"/merchant/orders/{order['order_id']}",
                 f"/merchant/orders/{order['order_id']}/status"):
        seen(path, api.get(path))

    secrets = {"admin token": ADMIN, "paypal secret": PAYPAL_SECRET, **{f"key {a}": k for a, k in api.keys.items()}}
    for label, text in bodies:
        for name, value in secrets.items():
            assert value not in text, f"{name} leaked in response {label}"
    # The grant token is delivered exactly once, to the owning agent, and nowhere else.
    for label, text in bodies:
        if label != "status":
            assert token not in text, f"grant token leaked in response {label}"
    assert token in delivery.text
    for label, text in bodies:
        assert not re.search(r"pact_gt_[A-Za-z0-9_-]{20,}", text) or label == "status"

    logs = caplog.text
    for name, value in {**secrets, "grant token": token}.items():
        assert value not in logs, f"{name} leaked in logs"

    # Audit trail and the database file hold only hashes.
    audit = json.dumps(api.get("/admin/audit", params={"limit": 500}, headers=ADMIN_H).json())
    for name, value in {**secrets, "grant token": token}.items():
        assert value not in audit, f"{name} leaked in audit"
    path = Path(api.db.engine.url.database)
    raw = b"".join(p.read_bytes() for p in (path, Path(f"{path}-wal")) if p.exists())
    for name, value in {**secrets, "grant token": token}.items():
        assert value.encode() not in raw, f"{name} stored in plaintext in the database"


# ===================================================================== 2. agent identity

def test_missing_malformed_and_unknown_credentials_are_rejected(api):
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, {"Authorization": "Bearer "},
                    {"Authorization": "Bearer pact_ak_unknown"}, {"Authorization": api.keys["ag_shopping"]}):
        r = api.get("/v1/budget", headers=headers)
        assert r.status_code == 401, headers
    # A key must not be accepted in the query string or a custom header.
    assert api.get("/v1/budget", params={"api_key": api.keys["ag_shopping"]}).status_code == 401
    assert api.get("/v1/budget", headers={"X-Api-Key": api.keys["ag_shopping"]}).status_code == 401


def test_disabled_agent_is_rejected_on_every_agent_route(api):
    order, req, token, _ = approved_grant(api)
    with api.db.transaction() as s:
        s.get(Agent, "ag_shopping").active = False
    rid = req["request_id"]
    calls = [("GET", "/v1/me", None), ("GET", "/v1/budget", None), ("GET", "/v1/audit", None),
             ("POST", "/v1/authorizations", auth_body(order)), ("POST", "/v1/authorizations/evaluate", auth_body(order)),
             ("GET", f"/v1/authorizations/{rid}", None), ("POST", f"/v1/authorizations/{rid}/revoke", None),
             ("POST", f"/v1/authorizations/{rid}/execute", exec_body(order, token))]
    for method, path, body in calls:
        r = api.request(method, path, headers=agent_h(api), json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "AGENT_INACTIVE", (path, r.text)
    # The grant was not consumed by the rejected attempt: re-enabling the agent can still use it.
    with api.db.transaction() as s:
        s.get(Agent, "ag_shopping").active = True
    assert api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api),
                    json=exec_body(order, token)).json()["state"] == "EXECUTING"


def test_rotated_key_stops_working_immediately(api):
    from pact_core.seed import rotate_agent_key

    old = api.keys["ag_shopping"]
    new = rotate_agent_key(api.db, "ag_shopping")
    assert api.get("/v1/me", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert api.get("/v1/me", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_cross_agent_access_is_denied_over_http(api):
    order, req, token, _ = approved_grant(api, agent="ag_shopping")
    rid = req["request_id"]
    other = agent_h(api, "ag_research")
    for method, path, body in [("GET", f"/v1/authorizations/{rid}", None),
                               ("POST", f"/v1/authorizations/{rid}/revoke", None),
                               ("POST", f"/v1/authorizations/{rid}/execute", exec_body(order, token))]:
        r = api.request(method, path, headers=other, json=body)
        assert r.status_code in (403, 404), (path, r.status_code)
        assert r.json()["error"]["code"] in {"REQUEST_NOT_FOUND", "GRANT_AGENT_MISMATCH"}
        assert token not in r.text
    assert api.get("/v1/audit", params={"request_id": rid}, headers=other).json() == {"events": []}
    # The owner is unaffected: the request is intact and its grant unused.
    assert api.get(f"/v1/authorizations/{rid}", headers=agent_h(api)).json()["state"] == "GRANTED"
    assert api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api),
                    json=exec_body(order, token)).json()["state"] == "EXECUTING"


def test_agent_cannot_request_authorization_for_someone_elses_identity(api):
    """Agent id and user id come from the key, never from the body (extra fields are rejected)."""
    order = make_order(api)
    for extra in ({"agent_id": "ag_research"}, {"user_id": "u_other"}):
        r = api.post("/v1/authorizations", headers=agent_h(api), json={**auth_body(order), **extra})
        assert r.status_code == 400 and r.json()["error"]["code"] == "INVALID_REQUEST"


# ===================================================================== admin boundary

ADMIN_ROUTES = [
    ("GET", "/admin/overview"), ("GET", "/admin/authorizations"), ("GET", "/admin/authorizations/x"),
    ("POST", "/admin/authorizations/x/approve"), ("POST", "/admin/authorizations/x/deny"),
    ("POST", "/admin/authorizations/x/revoke"), ("POST", "/admin/authorizations/x/cancel"),
    ("POST", "/admin/authorizations/x/reconcile"), ("GET", "/admin/policies"), ("PUT", "/admin/policies/system"),
    ("GET", "/admin/agents"), ("GET", "/admin/budgets"), ("GET", "/admin/audit"), ("GET", "/admin/audit/verify"),
]


def test_every_registered_admin_route_is_in_the_matrix(api):
    registered = {(m, p.replace("{request_id}", "x").replace("{policy_id}", "system"))
                  for route in api.app.routes if getattr(route, "path", "").startswith("/admin")
                  for m in route.methods for p in [route.path]}
    assert registered == set(ADMIN_ROUTES)


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_reject_agent_keys_and_bad_tokens(api, method, path):
    body = None if method == "GET" else {}
    bad = [agent_h(api), agent_h(api, "ag_storefront"), {}, {"Authorization": "Bearer wrong"},
           {"Authorization": f"Bearer {ADMIN[:-1]}"}, {"Authorization": f"Bearer {ADMIN}x"},
           {"Authorization": f"Bearer {ADMIN.upper()}"}]
    for headers in bad:
        r = api.request(method, path, headers=headers, json=body)
        assert r.status_code in (401, 403), (headers, r.status_code)
    # Token in the query string or another header is not a credential.
    assert api.request(method, path, params={"token": ADMIN, "admin_token": ADMIN}, json=body).status_code == 401
    assert api.request(method, path, headers={"X-Admin-Token": ADMIN}, json=body).status_code == 401


def test_agent_cannot_approve_its_own_request_so_nothing_is_granted(api):
    order = make_order(api)
    req = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(order)).json()
    r = api.post(f"/admin/authorizations/{req['request_id']}/approve", headers=agent_h(api),
                 json={"request_hash": req["request_hash"]})
    assert r.status_code == 403
    assert api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api)).json()["state"] == "PENDING_APPROVAL"


def test_admin_comparison_is_constant_time(api, monkeypatch):
    calls = []
    real = app_module.constant_time_equal
    monkeypatch.setattr(app_module, "constant_time_equal", lambda a, b: calls.append((a, b)) or real(a, b))
    assert api.get("/admin/overview", headers={"Authorization": "Bearer nope"}).status_code == 403
    assert calls == [("nope", ADMIN)]
    assert api.get("/admin/overview", headers=ADMIN_H).status_code == 200


def test_no_server_module_compares_secrets_with_plain_equality():
    """Static guard: secrets are compared via pact_core.crypto.constant_time_equal / hmac.compare_digest only."""
    from pact_core import crypto

    assert app_module.constant_time_equal is crypto.constant_time_equal
    assert core_module.constant_time_equal is crypto.constant_time_equal
    # Plaintext secrets (admin token, grant token, raw API key) must never meet ==/!=. (Lookups of a SHA-256 digest
    # in SQL are fine: the stored value is a hash of a 256-bit random key.)
    risky = re.compile(r"\b(admin_token|grant_token|claim\.token|inp\.grant_token|api_key|raw_token)\b\s*[!=]=|[!=]=\s*\b(admin_token|grant_token|api_key)\b")
    for path in [ROOT / "pact_server" / "app.py", ROOT / "pact_core" / "core.py", ROOT / "pact_core" / "grants.py",
                 ROOT / "pact_core" / "gateway.py", ROOT / "pact_core" / "authorization.py",
                 ROOT / "pact_server" / "storefront.py"]:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            assert not risky.search(line.split("#")[0]), f"{path.name}:{n} compares a secret with ==/!=: {line.strip()}"


def test_agent_key_lookup_uses_constant_time_compare(api, monkeypatch):
    calls = []
    real = core_module.constant_time_equal
    monkeypatch.setattr(core_module, "constant_time_equal", lambda a, b: calls.append((a, b)) or real(a, b))
    assert api.get("/v1/me", headers=agent_h(api)).status_code == 200
    assert calls, "agent authentication must go through constant_time_equal"


# ===================================================================== policy updates

def test_policy_update_requires_admin_and_rejects_invalid_rules(api):
    pol = next(p for p in api.get("/admin/policies", headers=ADMIN_H).json()["policies"] if p["id"] == "org:default")
    url = "/admin/policies/org:default"
    good = {"rules": pol["rules"], "expected_version": pol["version"]}
    assert api.put(url, headers=agent_h(api), json=good).status_code == 403
    assert api.put(url, json=good).status_code == 401

    bad_docs = [{**pol["rules"], "unknown_field": 1}, {**pol["rules"], "global_daily_limit_minor": -5},
                {**pol["rules"], "global_daily_limit_minor": "lots"}, {"approval_threshold_minor": []}]
    for rules in bad_docs:
        r = api.put(url, headers=ADMIN_H, json={"rules": rules, "expected_version": pol["version"]})
        assert r.status_code in (400, 422), rules
        assert r.json()["error"]["code"] in {"POLICY_INVALID", "INVALID_REQUEST"}
    stale = api.put(url, headers=ADMIN_H, json={"rules": pol["rules"], "expected_version": pol["version"] + 7})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "POLICY_VERSION_CONFLICT"
    assert api.put("/admin/policies/nope", headers=ADMIN_H, json=good).status_code == 404
    assert api.put(url, headers=ADMIN_H, json={**good, "extra": 1}).status_code == 422

    after = next(p for p in api.get("/admin/policies", headers=ADMIN_H).json()["policies"] if p["id"] == "org:default")
    assert after["rules"] == pol["rules"] and after["version"] == pol["version"]  # nothing partially applied
    ok = api.put(url, headers=ADMIN_H, json=good)
    assert ok.status_code == 200 and ok.json()["version"] == pol["version"] + 1
    events = api.get("/admin/audit", params={"event_type": "POLICY_UPDATED"}, headers=ADMIN_H).json()["events"]
    assert len(events) == 1


def test_there_is_no_agent_route_that_writes_policy_or_approves(api):
    for route in api.app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/v1"):
            assert not re.search(r"approve|deny|polic", path), path


# ===================================================================== 12 fail closed

def test_corrupt_policy_document_denies_over_http(api):
    with api.db.transaction() as s:
        s.get(Policy, "agent:ag_shopping").rules = {"max_transaction_minor": "NaN", "garbage": True}
    r = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(make_order(api, cheap=True)))
    assert r.status_code == 201 and r.json()["state"] == "DENIED"
    assert "grant_token" not in r.json()


def test_engine_exception_denies_and_never_grants(api, monkeypatch):
    import pact_core.authorization as authorization

    def boom(*a, **k):
        raise RuntimeError("policy engine exploded")

    monkeypatch.setattr(authorization, "evaluate", boom)
    r = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(make_order(api, cheap=True)))
    assert r.status_code in (201, 500)
    if r.status_code == 201:
        assert r.json()["state"] == "DENIED"
    budget = api.get("/admin/budgets", headers=ADMIN_H).json()
    assert all(scope["reserved_minor"] == 0 for scope in budget["scopes"])  # nothing was reserved
    assert api.get("/admin/overview", headers=ADMIN_H).json()["requests_by_state"].get("GRANTED", 0) == 0


# ===================================================================== 13 modification after approval

def test_order_modified_after_approval_is_rejected_at_execute(api):
    order, req, token, _ = approved_grant(api)
    products = api.get("/merchant/products", params={"merchant_id": order["merchant_id"], "sort": "price"}).json()["products"]
    cheaper = products[0]
    r = api.patch(f"/merchant/orders/{order['order_id']}", json={"items": [{"product_id": cheaper["product_id"], "quantity": 1}]})
    assert r.status_code == 200 and r.json()["total_minor"] != order["total_minor"]
    new_total = r.json()["total_minor"]
    rid = req["request_id"]
    # Re-presenting the approved values or the new live values both fail; the money is never moved.
    for body in (exec_body(order, token), exec_body(order, token, amount_minor=new_total)):
        e = api.post(f"/v1/authorizations/{rid}/execute", headers=agent_h(api), json=body)
        assert e.status_code == 409 and e.json()["error"]["code"] in {"ORDER_MODIFIED", "GRANT_BINDING_MISMATCH"}
    assert api.get(f"/v1/authorizations/{rid}", headers=agent_h(api)).json()["state"] == "GRANTED"
    events = api.get("/admin/audit", params={"event_type": "EXECUTION_REJECTED"}, headers=ADMIN_H).json()["events"]
    assert len(events) == 2


def test_approval_needs_the_displayed_hash_over_http(api):
    order = make_order(api)
    req = api.post("/v1/authorizations", headers=agent_h(api), json=auth_body(order)).json()
    r = api.post(f"/admin/authorizations/{req['request_id']}/approve", headers=ADMIN_H, json={"request_hash": "0" * 64})
    assert r.status_code >= 400
    assert api.get(f"/v1/authorizations/{req['request_id']}", headers=agent_h(api)).json()["state"] == "PENDING_APPROVAL"


# ===================================================================== prompt injection / MCP surface

def _mcp(api):
    return build_server(TestClient(api.app, headers=agent_h(api)))


def test_mcp_tools_cannot_approve_edit_policy_or_read_secrets(api):
    server = _mcp(api)
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    forbidden_words = ("approve", "approval", "deny", "policy", "admin", "secret", "token", "key", "env",
                       "config", "setting", "credential", "reconcile", "cancel", "rotate", "init", "shell", "exec_")
    for t in tools:
        # `pact_get_authorization_status`/`pact_execute_action` etc. are the audited exceptions below.
        hit = [w for w in forbidden_words if w in t.name]
        assert t.name in {"pact_evaluate_policy", "pact_execute_action", "pact_revoke_grant"} or not hit, (t.name, hit)
    assert "pact_evaluate_policy" in names  # dry run only: the next test proves it writes nothing
    # No tool takes an admin token, API key, URL or path argument that could redirect the credential.
    for t in tools:
        props = set((t.input_schema or {}).get("properties", {}))
        assert not props & {"admin_token", "token", "api_key", "key", "url", "path", "authorization", "headers",
                            "base_url", "agent_id", "user_id", "rules", "policy_id", "request_hash"}, (t.name, props)


def test_mcp_evaluate_policy_is_read_only(api):
    server = _mcp(api)
    order = make_order(api, cheap=True)
    before = api.get("/admin/overview", headers=ADMIN_H).json()["requests_by_state"]
    result = asyncio.run(server.call_tool("pact_evaluate_policy", {
        "order_id": order["order_id"], "merchant_id": order["merchant_id"],
        "amount": f"{order['total_minor'] / 100:.2f}", "currency": "USD"}))
    assert result is not None
    assert api.get("/admin/overview", headers=ADMIN_H).json()["requests_by_state"] == before


def test_mcp_client_holds_only_the_agent_key_and_gets_403_on_admin(api):
    """Even if a prompt-injected model could steer the HTTP client, its credential is an agent key."""
    http = TestClient(api.app, headers=agent_h(api))
    assert http.get("/admin/overview").status_code == 403
    assert http.post("/admin/authorizations/x/approve", json={"request_hash": "0" * 64}).status_code == 403
    src = (ROOT / "pact_mcp" / "server.py").read_text()
    assert "admin_token" not in src.lower() and "PACT_ADMIN_TOKEN" not in src
    assert "pact_core.db" not in src and "sqlalchemy" not in src and "PAYPAL" not in src


def test_mcp_responses_never_contain_secrets(api):
    server = _mcp(api)
    order = make_order(api, cheap=True)
    out = asyncio.run(server.call_tool("pact_request_authorization", {
        "order_id": order["order_id"], "merchant_id": order["merchant_id"],
        "amount": f"{order['total_minor'] / 100:.2f}", "currency": "USD"}))
    text = json.dumps(getattr(out, "structuredContent", None) or [c.text for c in out.content])
    for t in ("pact_get_budget", "pact_get_audit_log"):
        o = asyncio.run(server.call_tool(t, {}))
        text += json.dumps(getattr(o, "structuredContent", None) or [c.text for c in o.content])
    for secret in [ADMIN, PAYPAL_SECRET, *api.keys.values()]:
        assert secret not in text


# ===================================================================== storefront is not an admin surface

def test_storefront_source_never_touches_the_admin_api_or_token():
    shop = ROOT / "dashboard" / "src" / "shop"
    files = [*shop.rglob("*.ts"), *shop.rglob("*.tsx")]
    assert files
    api_use = re.compile(r"adminApi|adminToken|lib/admin|\.\./admin|['\"`]/admin|fetch\([^)]*admin|sessionStorage")
    for path in files:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            assert not api_use.search(line), f"{path.name}:{n} references the admin API/token: {line.strip()}"
    # `#/admin` may only appear as display text (JSX content), never as a link target or import.
    for path in files:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "#/admin" in line:
                assert not re.search(r"(href|to|src|navigate|location)\W", line.split("#/admin")[0][-40:]), \
                    f"{path.name}:{n}: {line.strip()}"


def test_storefront_cannot_start_anything_but_pact_governed_payments(api):
    """Web checkout goes through PACT: with no approval the order stays unpaid and nothing is granted."""
    order = make_order(api, buyer="web_visitor")  # above the approval threshold
    r = api.post(f"/merchant/orders/{order['order_id']}/checkout")
    assert r.status_code == 200 and r.json()["state"] == "PENDING_APPROVAL"
    assert r.json()["checkout_url"] is None
    assert api.get(f"/merchant/orders/{order['order_id']}").json()["payment_status"] != "CAPTURED"
    for _ in range(3):  # hammering checkout never approves
        assert api.post(f"/merchant/orders/{order['order_id']}/checkout").json()["state"] == "PENDING_APPROVAL"
    assert "grant_token" not in json.dumps(r.json())


# ===================================================================== CORS / headers / input hardening

def test_no_cors_headers_are_emitted(api):
    r = api.options("/admin/overview", headers={"Origin": "https://evil.example",
                                                 "Access-Control-Request-Method": "GET",
                                                 "Access-Control-Request-Headers": "authorization"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    r = api.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_error_bodies_do_not_leak_internals(api):
    r = api.post("/v1/authorizations", headers=agent_h(api), json={"merchant_id": ["x"], "order_id": 5})
    assert r.status_code in (400, 422)
    text = r.text
    assert "Traceback" not in text and "sqlalchemy" not in text.lower() and ".py" not in text
    assert api.get("/merchant/orders/%27%20OR%201%3D1--").status_code == 404
    assert api.get("/merchant/products", params={"q": "%' OR '1'='1"}).status_code == 200


def test_oversized_and_unknown_fields_are_rejected(api):
    assert api.post("/merchant/orders", json={"items": [{"product_id": "x", "quantity": 1}], "admin": True}).status_code == 422
    assert api.post("/admin/authorizations/x/approve", headers=ADMIN_H,
                    json={"request_hash": "0" * 64, "note": "n" * 501}).status_code == 422
    assert api.get("/payments/return", params={"token": "t" * 65}).status_code == 422


def test_reflected_values_are_html_escaped(api):
    r = api.get("/sim/checkout/%3Cscript%3Ealert(1)%3C%2Fscript%3E")
    assert r.status_code == 404 and "<script>" not in r.text
    r = api.get("/payments/return", params={"token": "<img src=x onerror=alert(1)>"})
    assert "<img" not in r.text


def test_static_dashboard_mount_does_not_serve_files_outside_dist(api):
    for path in ("/../pact.db", "/..%2fpact.db", "/%2e%2e/pyproject.toml", "/assets/../../pyproject.toml"):
        r = api.get(path)
        assert "[project]" not in r.text and not r.content.startswith(b"SQLite format")


# ===================================================================== MCP path handling

@pytest.mark.xfail(strict=True, reason="SEC-3: MCP tool ids are interpolated into URL paths unescaped, so '../' "
                                       "re-targets the agent's authenticated request to another API route")
def test_mcp_ids_cannot_traverse_to_other_api_paths(api):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    http = httpx.Client(base_url="http://pact.test", transport=httpx.MockTransport(handler))
    server = build_server(http)
    asyncio.run(server.call_tool("merchant_get_product", {"product_id": "../../v1/me"}))
    assert seen and seen[-1].startswith("/merchant/products/"), seen
    assert ".." not in seen[-1]


# ===================================================================== confirmed, not-yet-fixed findings (docs/SECURITY.md)

def _live_web_payment(api):
    """A cheap web order that went straight to the (simulated) provider checkout: state EXECUTING."""
    order = make_order(api, buyer="web_visitor", cheap=True)
    r = api.post(f"/merchant/orders/{order['order_id']}/checkout").json()
    assert r["state"] == "EXECUTING", r
    return order, r


def test_anonymous_party_cannot_cancel_someone_elses_live_payment(api):
    """SEC-1, GET half: fixed by #40. The cancel link only records that the buyer left; it ends nothing."""
    order, r = _live_web_payment(api)
    provider_order_id = api.get(f"/merchant/orders/{order['order_id']}/status").json()["authorization"]["provider_order_id"]
    assert provider_order_id
    attacker = TestClient(api.app)  # no cookies, no credentials, never saw the buyer's session
    attacker.get("/payments/cancel", params={"token": provider_order_id})
    state = api.get(f"/admin/authorizations/{r['request_id']}", headers=ADMIN_H).json()["state"]
    assert state == "EXECUTING", "a third party aborted a payment it does not own"


@pytest.mark.xfail(strict=True, reason="SEC-1 (remaining): POST /payments/abandon is unauthenticated and trusts the bare "
                                       "provider order id (leaked by the public order-status route), so anyone can "
                                       "still end a live, uncaptured payment and release its budget")
def test_anonymous_party_cannot_abandon_someone_elses_live_payment(api):
    order, r = _live_web_payment(api)
    provider_order_id = api.get(f"/merchant/orders/{order['order_id']}/status").json()["authorization"]["provider_order_id"]
    attacker = TestClient(api.app)
    attacker.post("/payments/abandon", data={"token": provider_order_id})
    state = api.get(f"/admin/authorizations/{r['request_id']}", headers=ADMIN_H).json()["state"]
    assert state == "EXECUTING", "a third party aborted a payment it does not own"


@pytest.mark.xfail(strict=True, reason="SEC-2: no X-Frame-Options / CSP frame-ancestors / nosniff on any response, "
                                       "so the dashboard (admin Approve button) and checkout pages can be framed")
def test_responses_forbid_framing_and_sniffing(api):
    for path in ("/", "/payments/return?token=x", "/health"):
        h = {k.lower(): v for k, v in api.get(path).headers.items()}
        assert "x-frame-options" in h or "frame-ancestors" in h.get("content-security-policy", ""), path
        assert h.get("x-content-type-options") == "nosniff", path


def test_weak_or_placeholder_admin_tokens_are_rejected_at_startup():
    for weak in ("x", "admin", "short-token-123", "change-me-to-a-long-random-string", "ChangeMe" + "x" * 30):
        with pytest.raises(ValueError) as exc:
            Settings(_env_file=None, PACT_ADMIN_TOKEN=weak, PACT_DATABASE_URL="sqlite://")
        assert weak not in str(exc.value)  # the rejected value is never echoed
    Settings(_env_file=None, PACT_ADMIN_TOKEN="a-strong-random-token-0123456789", PACT_DATABASE_URL="sqlite://")


@pytest.mark.xfail(strict=True, reason="SEC-5: no rate limiting or lockout on admin/agent authentication, so the "
                                       "admin token can be brute-forced online")
def test_repeated_bad_admin_tokens_are_throttled(api):
    codes = {api.get("/admin/overview", headers={"Authorization": f"Bearer guess-{i}"}).status_code for i in range(120)}
    assert 429 in codes


@pytest.mark.xfail(strict=True, reason="SEC-6: StorefrontService allocates a per-order lock before checking that "
                                       "the order exists, so unauthenticated random ids grow memory without bound")
def test_unknown_order_ids_do_not_allocate_server_state(api):
    import gc

    from pact_server.storefront import StorefrontService

    service = next(o for o in gc.get_objects()
                   if isinstance(o, StorefrontService) and o.core is api.app.state.core)
    before = len(service._locks)
    for i in range(25):
        assert api.post(f"/merchant/orders/ord_nonexistent_{i}/checkout").status_code == 404
    assert len(service._locks) == before
