"""#36 Demo convenience: wait tool, approval CLI, doctor checks, server lock, agent guardrails."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from adapters.simulation.adapter import SimulationAdapter
from merchant_lab.seed import seed_catalogue
from pact_core.config import Settings
from pact_core.seed import rotate_agent_key, seed_agents, seed_policies
from pact_mcp.server import INSTRUCTIONS, build_server
from pact_server import ops
from pact_server.app import create_app

ADMIN = "test-admin-token-0123456789abcdef"
PUBLIC = "http://testserver"


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
    return {"mcp": build_server(agent_http, poll_seconds=0.05), "admin": admin_http, "sim": sim,
            "approvals": ops.ApprovalClient(admin_http), "db": db, "keys": keys, "app": app}


def call(server, name, **args) -> dict:
    result = asyncio.run(server.call_tool(name, args))
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if data:
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def keyboard_request(mcp) -> tuple[dict, dict]:
    kb = call(mcp, "merchant_search_products", query="Atlas")["products"][0]
    call(mcp, "merchant_add_to_cart", product_id=kb["product_id"])
    order = call(mcp, "merchant_checkout_cart")
    req = call(mcp, "pact_request_authorization", order_id=order["order_id"], merchant_id=order["merchant_id"],
               amount=order["total"])
    return order, req


def later(delay, fn):
    t = threading.Thread(target=lambda: (time.sleep(delay), fn()))
    t.start()
    return t


# ---------------------------------------------------------------- wait tool

def test_wait_returns_grant_after_human_approval(stack):
    mcp, approvals = stack["mcp"], stack["approvals"]
    order, req = keyboard_request(mcp)
    assert req["state"] == "PENDING_APPROVAL"
    t = later(0.3, lambda: approvals.approve(req["request_id"], req["request_hash"]))
    waited = call(mcp, "pact_wait_for_status", request_id=req["request_id"], timeout_seconds=20)
    t.join()
    assert waited["state"] == "GRANTED" and waited["grant_token"].startswith("pact_gr_")
    assert waited["wait"]["timed_out"] is False and waited["wait"]["polls"] >= 2


def test_wait_returns_executed_after_buyer_pays(stack):
    mcp, approvals, sim = stack["mcp"], stack["approvals"], stack["sim"]
    order, req = keyboard_request(mcp)
    approvals.approve(req["request_id"], req["request_hash"])
    granted = call(mcp, "pact_wait_for_status", request_id=req["request_id"], timeout_seconds=5)
    ex = call(mcp, "pact_execute_action", request_id=req["request_id"], grant_token=granted["grant_token"],
              order_id=order["order_id"], merchant_id=order["merchant_id"], amount=order["total"])
    t = later(0.3, lambda: sim.buyer_approve(ex["execution"]["provider_order_id"]))
    done = call(mcp, "pact_wait_for_status", request_id=req["request_id"], timeout_seconds=20)
    t.join()
    assert done["state"] == "EXECUTED" and done["execution"]["capture_id"]


def test_wait_times_out_cleanly(stack):
    order, req = keyboard_request(stack["mcp"])
    r = call(stack["mcp"], "pact_wait_for_status", request_id=req["request_id"], timeout_seconds=1)
    assert r["state"] == "PENDING_APPROVAL" and r["wait"]["timed_out"] is True and "hint" in r["wait"]


def test_wait_returns_immediately_for_terminal_or_denied(stack):
    mcp = stack["mcp"]
    cheap = call(mcp, "merchant_search_products", query="unbranded")["products"][0]
    order = call(mcp, "merchant_create_order", items=[{"product_id": cheap["product_id"], "quantity": 1}])
    req = call(mcp, "pact_request_authorization", order_id=order["order_id"], merchant_id=order["merchant_id"],
               amount=order["total"])
    r = call(mcp, "pact_wait_for_status", request_id=req["request_id"], timeout_seconds=30)
    assert r["state"] == "DENIED" and r["wait"]["polls"] == 1
    assert call(mcp, "pact_wait_for_status", request_id="auth_missing")["error"]["code"] == "REQUEST_NOT_FOUND"


def test_instructions_forbid_self_management():
    for phrase in ("pact_wait_for_status", "Never edit PACT's .env", "pact approve", "pact doctor"):
        assert phrase in INSTRUCTIONS


# ---------------------------------------------------------------- approval CLI

def test_approval_client_and_describe(stack):
    order, req = keyboard_request(stack["mcp"])
    client = stack["approvals"]
    [p] = client.pending()
    text = ops.describe(client.detail(p["request_id"]))
    assert "79.00 USD" in text and "TechMart" in text and "Atlas TKL" in text and "USER_APPROVAL_THRESHOLD" in text
    with pytest.raises(RuntimeError, match="APPROVAL_HASH_MISMATCH"):
        client.approve(req["request_id"], "0" * 64)
    assert client.approve(req["request_id"], req["request_hash"])["state"] == "APPROVED"


def test_cli_approve_selects_single_pending(stack, monkeypatch, capsys):
    from pact_server import cli

    order, req = keyboard_request(stack["mcp"])
    monkeypatch.setattr(cli, "_settings", lambda: None)
    monkeypatch.setattr(ops.ApprovalClient, "from_settings", classmethod(lambda cls, s: stack["approvals"]))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["approve"]) == 1  # non-interactive without --yes is refused
    assert "--yes" in capsys.readouterr().out
    assert cli.main(["approve", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Atlas TKL" in out and "APPROVED" in out
    assert cli.main(["approvals"]) == 0 and "No payments" in capsys.readouterr().out


def test_cli_approve_requires_choice_when_several_pending(stack, monkeypatch, capsys):
    from pact_server import cli

    keyboard_request(stack["mcp"])
    mcp = stack["mcp"]
    hp = call(mcp, "merchant_search_products", query="Headphones")["products"][0]
    o = call(mcp, "merchant_create_order", items=[{"product_id": hp["product_id"], "quantity": 1}])
    call(mcp, "pact_request_authorization", order_id=o["order_id"], merchant_id=o["merchant_id"], amount=o["total"])
    monkeypatch.setattr(cli, "_settings", lambda: None)
    monkeypatch.setattr(ops.ApprovalClient, "from_settings", classmethod(lambda cls, s: stack["approvals"]))
    assert cli.main(["approve", "--yes"]) == 1
    assert "2 payments are waiting" in capsys.readouterr().out


def test_cli_deny(stack, monkeypatch, capsys):
    from pact_server import cli

    order, req = keyboard_request(stack["mcp"])
    monkeypatch.setattr(cli, "_settings", lambda: None)
    monkeypatch.setattr(ops.ApprovalClient, "from_settings", classmethod(lambda cls, s: stack["approvals"]))
    assert cli.main(["deny", req["request_id"], "too", "expensive"]) == 0
    assert "DENIED" in capsys.readouterr().out


# ---------------------------------------------------------------- doctor

def test_doctor_detects_provider_mismatch_and_missing_keys(stack, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env, no .pact/agent_keys.json here
    settings = Settings(_env_file=None, PACT_ADMIN_TOKEN=ADMIN, PACT_PAYMENT_PROVIDER="simulation",
                        PACT_DATABASE_URL=f"sqlite:///{tmp_path / 'doc.db'}")
    checks = {c.name: c for c in ops.run_doctor(settings, check_mcp=False, http=TestClient(stack["app"]))}
    assert checks["PACT server"].ok  # running app reports simulation == settings
    assert not checks["Agent keys file"].ok and "pact demo" in checks["Agent keys file"].fix
    assert not checks[".env file"].ok
    assert not checks["Database seeded"].ok

    mismatch = Settings(_env_file=None, PACT_ADMIN_TOKEN=ADMIN, PACT_PAYMENT_PROVIDER="paypal",
                        PAYPAL_CLIENT_ID="x", PAYPAL_CLIENT_SECRET="y", PAYPAL_BASE_URL="http://localhost:1",
                        PACT_DATABASE_URL=f"sqlite:///{tmp_path / 'doc.db'}")
    checks = {c.name: c for c in ops.run_doctor(mismatch, check_mcp=False, http=TestClient(stack["app"]))}
    assert not checks["PACT server"].ok and "pact stop" in checks["PACT server"].fix
    assert not checks["PayPal Sandbox credentials"].ok
    report, healthy = ops.format_checks(list(checks.values()))
    assert not healthy and "✗" in report


def test_doctor_reports_stopped_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=None, PACT_DATABASE_URL=f"sqlite:///{tmp_path / 'd.db'}")
    dead = httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
                        base_url="http://localhost:9")
    checks = {c.name: c for c in ops.run_doctor(settings, check_mcp=False, http=dead)}
    assert not checks["PACT server"].ok and "pact serve" in checks["PACT server"].fix


def test_rotate_agent_key(stack):
    from pact_core.core import PactCore  # noqa: F401  (import check)

    new = rotate_agent_key(stack["db"], "ag_shopping")
    core = stack["app"].state.core
    assert core.authenticate_agent(new).id == "ag_shopping"
    with pytest.raises(Exception):
        core.authenticate_agent(stack["keys"]["ag_shopping"])


# ---------------------------------------------------------------- server lock

def test_lock_refuses_second_server(tmp_path):
    lock = tmp_path / "server.lock"
    ops.acquire_lock("127.0.0.1", 0, "paypal", lock_file=lock)
    assert ops.read_lock(lock)["pid"] == os.getpid()
    with pytest.raises(ops.ServerAlreadyRunning, match="already running"):
        ops.acquire_lock("127.0.0.1", 0, "simulation", lock_file=lock)
    ops.release_lock(lock)
    assert not lock.exists()


def test_stale_lock_is_ignored(tmp_path):
    lock = tmp_path / "server.lock"
    lock.write_text(json.dumps({"pid": 999999999, "port": 8000, "provider": "paypal"}))
    assert ops.read_lock(lock) is None
    ops.acquire_lock("127.0.0.1", 0, "paypal", lock_file=lock)
    ops.release_lock(lock)


def test_lock_refuses_busy_port(tmp_path):
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        with pytest.raises(ops.ServerAlreadyRunning, match="in use"):
            ops.acquire_lock("127.0.0.1", srv.getsockname()[1], "paypal", lock_file=tmp_path / "l")
    finally:
        srv.close()


def test_shopper_workspace(tmp_path):
    ops.prepare_shopper_dir(tmp_path / "shop")
    md = (tmp_path / "shop" / "CLAUDE.md").read_text()
    assert "pact_wait_for_status" in md and "cannot approve" in md
