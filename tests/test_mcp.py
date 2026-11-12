"""#13 MCP server: tool surface, no approval tool, and the full purchase driven through MCP tools."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.seed import seed_catalogue
from pact_core.config import Settings
from pact_core.seed import seed_agents, seed_policies
from pact_mcp.server import INSTRUCTIONS, build_server
from pact_server.app import create_app

ADMIN = "test-admin-token-0123456789abcdef"
PUBLIC = "http://testserver"
EXPECTED_TOOLS = {
    "merchant_search_products", "merchant_get_product", "merchant_create_order", "merchant_get_order",
    "merchant_list_transactions", "merchant_add_to_cart", "merchant_view_cart", "merchant_checkout_cart",
    "pact_request_authorization", "pact_evaluate_policy", "pact_get_authorization_status", "pact_execute_action",
    "pact_revoke_grant", "pact_get_audit_log", "pact_get_budget", "pact_wait_for_status",
}


@pytest.fixture
def stack(db, clock):
    settings = Settings(_env_file=None, PACT_ADMIN_TOKEN=ADMIN, PACT_PUBLIC_URL=PUBLIC, PACT_DATABASE_URL="sqlite://")
    sim = SimulationAdapter(PUBLIC)
    app = create_app(settings, adapters={"simulation": sim}, clock=clock, start_sweeper=False, db=db)
    seed_catalogue(db)
    seed_policies(db)
    keys = seed_agents(db)
    agent_http = TestClient(app, headers={"Authorization": f"Bearer {keys['ag_shopping']}"})
    admin_http = TestClient(app, headers={"Authorization": f"Bearer {ADMIN}"})
    return build_server(agent_http), admin_http, sim


def call(server, name, **args) -> dict:
    result = asyncio.run(server.call_tool(name, args))
    if getattr(result, "structuredContent", None):
        data = result.structuredContent
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def test_tool_surface_has_no_approval_capability(stack):
    server, _, _ = stack
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == EXPECTED_TOOLS
    assert not any(word in n for n in names for word in ("approve", "deny", "policy_update", "admin"))
    assert all(len(n) <= 64 and n.replace("_", "").isalnum() for n in names)  # Claude tool-name rules


def test_instructions_cover_checkout_retry_and_failure_reasons(stack):
    # The existing rules keep their numbers; the checkout guidance is appended as rules 8 and 9.
    for n in range(1, 10):
        assert f"\n{n}. " in INSTRUCTIONS
    for phrase in ("BUYER_CANCELLED", "INSTRUMENT_DECLINED", "SAME approval_url", "pact_wait_for_status",
                   "terminal state (FAILED, EXPIRED, REVOKED, DENIED)", "failure_reason exactly as returned",
                   "CHECKOUT_TIMEOUT", "CAPTURE_REJECTED:<code>", "PROVIDER_ORDER_NOT_FOUND"):
        assert phrase in INSTRUCTIONS
    server, _, _ = stack
    tools = {t.name: t.description for t in asyncio.run(server.list_tools())}
    assert "same approval_url" in tools["pact_wait_for_status"] and "failure_reason" in tools["pact_wait_for_status"]
    assert "same approval_url" in tools["pact_execute_action"]


def test_full_purchase_via_mcp_tools(stack):
    server, admin, sim = stack
    products = call(server, "merchant_search_products", query="keyboard", max_price="100.00", sort="rating")["products"]
    best = next(p for p in products if p["merchant_id"] == "m_techmart")
    assert best["name"].startswith("Atlas")

    call(server, "merchant_add_to_cart", product_id=best["product_id"])
    assert call(server, "merchant_view_cart")["total_minor"] == best["price_minor"]
    order = call(server, "merchant_checkout_cart")

    req = call(server, "pact_request_authorization", order_id=order["order_id"], merchant_id=order["merchant_id"],
               amount=order["total"], description=best["name"])
    assert req["state"] == "PENDING_APPROVAL"
    assert "dashboard" in req["next_action"]

    # The human approves in the dashboard (admin API) — no MCP tool can do this.
    admin.post(f"/admin/authorizations/{req['request_id']}/approve", json={"request_hash": req["request_hash"]})
    granted = call(server, "pact_get_authorization_status", request_id=req["request_id"])
    assert granted["state"] == "GRANTED" and granted["grant_token"]

    ex = call(server, "pact_execute_action", request_id=req["request_id"], grant_token=granted["grant_token"],
              order_id=order["order_id"], merchant_id=order["merchant_id"], amount=order["total"])
    assert ex["state"] == "EXECUTING" and ex["execution"]["approval_url"]

    sim.buyer_approve(ex["execution"]["provider_order_id"])  # user completes checkout
    final = call(server, "pact_get_authorization_status", request_id=req["request_id"])
    assert final["state"] == "EXECUTED"

    replay = call(server, "pact_execute_action", request_id=req["request_id"], grant_token=granted["grant_token"],
                  order_id=order["order_id"], merchant_id=order["merchant_id"], amount=order["total"])
    assert replay["error"]["code"] == "GRANT_ALREADY_USED"

    trail = [e["event_type"] for e in call(server, "pact_get_audit_log", request_id=req["request_id"])["events"]]
    assert trail[0] == "REPLAY_REJECTED" and "PAYMENT_CAPTURED" in trail
    budget = {s["scope"]: s for s in call(server, "pact_get_budget")["scopes"]}
    assert budget["org:global"]["settled_minor"] == best["price_minor"]


def test_tampered_amount_is_rejected_via_mcp(stack):
    server, admin, _ = stack
    mouse = call(server, "merchant_search_products", query="mouse")["products"][0]
    order = call(server, "merchant_create_order", items=[{"product_id": mouse["product_id"], "quantity": 1}])
    req = call(server, "pact_request_authorization", order_id=order["order_id"], merchant_id=order["merchant_id"],
               amount=order["total"])
    assert req["state"] == "GRANTED"  # $29 is below the approval threshold
    bad = call(server, "pact_execute_action", request_id=req["request_id"], grant_token=req["grant_token"],
               order_id=order["order_id"], merchant_id=order["merchant_id"], amount="0.01")
    assert bad["error"]["code"] == "GRANT_BINDING_MISMATCH"


def test_denied_merchant_and_dry_run(stack):
    server, _, _ = stack
    cheap = next(p for p in call(server, "merchant_search_products", query="unbranded")["products"])
    order = call(server, "merchant_create_order", items=[{"product_id": cheap["product_id"], "quantity": 1}])
    dry = call(server, "pact_evaluate_policy", order_id=order["order_id"], merchant_id=order["merchant_id"],
               amount=order["total"])
    assert dry["dry_run"] and dry["decision"]["decision"] == "DENY"
    req = call(server, "pact_request_authorization", order_id=order["order_id"], merchant_id=order["merchant_id"],
               amount=order["total"])
    assert req["state"] == "DENIED" and "MERCHANT_NOT_APPROVED" in req["decision"]["reason_codes"]


def test_bad_amount_and_revoke(stack):
    server, _, _ = stack
    assert call(server, "pact_request_authorization", order_id="o", merchant_id="m",
                amount="lots")["error"]["code"] == "INVALID_AMOUNT"
    kb = next(p for p in call(server, "merchant_search_products", query="Atlas")["products"])
    order = call(server, "merchant_create_order", items=[{"product_id": kb["product_id"], "quantity": 1}])
    req = call(server, "pact_request_authorization", order_id=order["order_id"], merchant_id=order["merchant_id"],
               amount=order["total"])
    assert call(server, "pact_revoke_grant", request_id=req["request_id"])["state"] == "REVOKED"
