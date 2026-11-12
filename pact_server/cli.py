"""``pact`` command-line interface.

    pact demo                 one command: check config, seed, set up the shopper workspace + MCP, serve
    pact doctor               diagnose configuration, PayPal credentials, server and MCP registration
    pact approvals            list payments waiting for your approval
    pact approve [ID] [--yes] approve exactly what is shown (the only pending one if ID is omitted)
    pact deny ID [REASON]     deny a pending payment
    pact cancel [ID]          cancel a payment stuck in checkout (checks the provider first; lists them if no ID)
    pact init [--reset]       create tables and seed catalogue, policies and demo agents
    pact serve [--port N]     run the API (+ dashboard if built); refuses to start a second server
    pact stop                 stop the running server
    pact verify-audit         verify the tamper-evident audit chain
"""

from __future__ import annotations

import argparse
import atexit
import json
import sys
from pathlib import Path

from pact_server import ops

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SHOPPER_DIR = Path.home() / "pact-shopper"


def _settings():
    from pact_core.config import get_settings

    return get_settings()


def _seed(settings, reset: bool = False) -> dict:
    import merchant_lab.models  # noqa: F401
    from merchant_lab.seed import seed_catalogue
    from pact_core.db import Database
    from pact_core.seed import seed_agents, seed_policies

    db = Database(settings.database_url)
    if reset:
        db.drop_all()
        if ops.KEYS_FILE.exists():
            ops.KEYS_FILE.unlink()
    db.create_all()
    catalogue = seed_catalogue(db)
    policies = seed_policies(db)
    keys = seed_agents(db)
    if keys:
        ops.save_keys(keys)
    return {"db": db, "catalogue": catalogue, "policies": policies, "new_keys": keys}


# ---------------------------------------------------------------------- commands


def _init(args) -> int:
    settings = _settings()
    r = _seed(settings, reset=args.reset)
    print(f"Database:  {settings.database_url}")
    print(f"Catalogue: {r['catalogue']['merchants_total']} merchants, {r['catalogue']['products_total']} products")
    print(f"Policies:  {len(r['policies'])} loaded" if r["policies"] else "Policies:  already present (kept)")
    if r["new_keys"]:
        print(f"Agents:    {len(r['new_keys'])} created; API keys written to {ops.KEYS_FILE} (mode 600). "
              "Only hashes are stored in the DB.")
    else:
        print(f"Agents:    already present; keys are in {ops.KEYS_FILE}")
    if not settings.admin_token:
        print("WARNING:   PACT_ADMIN_TOKEN is not set; approvals will be disabled.", file=sys.stderr)
    print(f"Provider:  {settings.payment_provider}  ·  currency {settings.currency}")
    return 0


def _serve(args) -> int:
    import uvicorn

    settings = _settings()
    try:
        ops.acquire_lock(args.host, args.port, settings.payment_provider)
    except ops.ServerAlreadyRunning as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    atexit.register(ops.release_lock)
    try:
        uvicorn.run("pact_server.app:create_app", factory=True, host=args.host, port=args.port, reload=args.reload)
    finally:
        ops.release_lock()
    return 0


def _stop(_args) -> int:
    print(ops.stop_server())
    return 0


def _doctor(args) -> int:
    settings = _settings()
    shopper = Path(args.shopper_dir).expanduser() if args.shopper_dir else None
    report, healthy = ops.format_checks(ops.run_doctor(settings, check_mcp=not args.no_mcp, shopper_dir=shopper))
    print("PACT doctor\n" + report)
    print("\nAll good." if healthy else "\nSome checks failed — see the → fixes above.")
    return 0 if healthy else 1


def _demo(args) -> int:
    from pact_core.seed import rotate_agent_key

    settings = _settings()
    print("PACT demo setup")
    problems = [c for c in ops.run_doctor(settings, check_mcp=False) if c.name in
                (".env file", "Admin token", "PayPal Sandbox credentials") and not c.ok]
    if problems:
        print(ops.format_checks(problems)[0])
        print("\nFix the items above, then run `pact demo` again.")
        return 1
    r = _seed(settings)
    print(f" ✓ data          {r['catalogue']['products_total']} products, policies and demo agents ready")

    keys = ops.load_keys()
    rotated = False
    if ops.SHOPPING_AGENT not in keys:
        keys[ops.SHOPPING_AGENT] = rotate_agent_key(r["db"], ops.SHOPPING_AGENT)
        ops.save_keys({ops.SHOPPING_AGENT: keys[ops.SHOPPING_AGENT]})
        rotated = True
        print(f" ✓ agent key     issued a new key for {ops.SHOPPING_AGENT} ({ops.KEYS_FILE})")

    shopper = Path(args.shopper_dir).expanduser()
    if not args.no_mcp:
        ops.prepare_shopper_dir(shopper)
        msg = ops.register_mcp(shopper, PROJECT_DIR, settings.public_url, keys[ops.SHOPPING_AGENT], replace=rotated)
        print(f" ✓ shopper       {msg}")

    provider = "PayPal Sandbox" if settings.payment_provider == "paypal" else "SIMULATION (not PayPal)"
    print(f"""
Ready. Payments go through: {provider}

  1. In a new terminal:   cd {shopper} && claude
     Ask: "Find the best mechanical keyboard under $100 and buy it."
  2. When Claude says it is waiting for approval:   pact approve
  3. Open the checkout link Claude gives you and pay
     (PayPal Sandbox: log in with your *personal* sandbox account, not the business one).
  Claude continues on its own after steps 2 and 3. Diagnose anything with `pact doctor`.
""")
    if args.no_serve:
        return 0
    running = ops.read_lock()
    if running:
        print(f"Server already running (pid {running['pid']}, provider {running['provider']}).")
        if running["provider"] != settings.payment_provider:
            print(f"  ! .env says {settings.payment_provider}: run `pact stop` and `pact demo` again.")
        return 0
    print(f"Starting PACT on {settings.public_url} (Ctrl+C to stop)…\n")
    return _serve(argparse.Namespace(host=args.host, port=args.port, reload=False))


def _approvals(_args) -> int:
    client = ops.ApprovalClient.from_settings(_settings())
    pending = client.pending()
    if not pending:
        print("No payments are waiting for approval.")
        return 0
    for p in pending:
        print(ops.describe(client.detail(p["request_id"])) + "\n")
    print("Approve with:  pact approve <request_id>" if len(pending) > 1 else "Approve with:  pact approve")
    return 0


def _approve(args) -> int:
    client = ops.ApprovalClient.from_settings(_settings())
    rid = args.request_id
    if rid is None:
        pending = client.pending()
        if not pending:
            print("No payments are waiting for approval.")
            return 0
        if len(pending) > 1:
            print(f"{len(pending)} payments are waiting; choose one:\n")
            for p in pending:
                print(ops.describe(client.detail(p["request_id"])) + "\n")
            return 1
        rid = pending[0]["request_id"]
    detail = client.detail(rid)
    print(ops.describe(detail))
    if detail["state"] != "PENDING_APPROVAL":
        print(f"\nThis request is {detail['state']}, not waiting for approval.")
        return 1
    if not args.yes:
        if not sys.stdin.isatty():
            print("\nRe-run with --yes to approve non-interactively.")
            return 1
        if input(f"\nApprove {detail['amount']} {detail['currency']} to {detail.get('merchant_name')}? [y/N] ").strip().lower() != "y":
            print("Not approved.")
            return 1
    try:
        result = client.approve(rid, detail["request_hash"])  # bound to exactly what was displayed
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ {rid} -> {result['state']}. The agent will continue automatically.")
    return 0


def _deny(args) -> int:
    client = ops.ApprovalClient.from_settings(_settings())
    try:
        result = client.deny(args.request_id, " ".join(args.reason) or "denied from terminal")
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ {args.request_id} -> {result['state']}")
    return 0


def _cancel(args) -> int:
    client = ops.ApprovalClient.from_settings(_settings())
    if not args.request_id:
        stuck = client.stuck()
        if not stuck:
            print("No payments are in progress.")
            return 0
        for p in stuck:
            print(f"{p['request_id']}  {p['amount']} {p['currency']}  {p['state']}  provider={p['execution']['provider']} "
                  f"status={p['execution']['provider_status']}")
        print("\nCancel with:  pact cancel <request_id>")
        return 0
    try:
        result = client.cancel(args.request_id)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 1
    ex = result["execution"]
    if result["state"] == "EXECUTED":
        print(f"! {args.request_id} had already been paid at the provider; it was completed instead (capture {ex['capture_id']}).")
    else:
        print(f"✓ {args.request_id} -> {result['state']} ({ex['failure_reason']}); its budget reservation was released.")
    return 0


def _verify(_args) -> int:
    from pact_core import audit
    from pact_core.db import Database

    db = Database(_settings().database_url)
    with db.transaction() as s:
        result = audit.verify_chain(s)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pact", description="PACT — Policy and Authorization Control Tower")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("demo", help="check config, seed, set up the shopper workspace and MCP, then serve")
    p.add_argument("--shopper-dir", default=str(DEFAULT_SHOPPER_DIR), help="workspace for the shopping agent")
    p.add_argument("--no-mcp", action="store_true", help="don't register the MCP server with Claude Code")
    p.add_argument("--no-serve", action="store_true", help="set up only, don't start the server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_demo)

    p = sub.add_parser("doctor", help="diagnose configuration and runtime")
    p.add_argument("--shopper-dir", default=None, help="also check MCP registration for this workspace")
    p.add_argument("--no-mcp", action="store_true")
    p.set_defaults(func=_doctor)

    sub.add_parser("approvals", help="list payments waiting for approval").set_defaults(func=_approvals)
    p = sub.add_parser("approve", help="approve a pending payment")
    p.add_argument("request_id", nargs="?")
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=_approve)
    p = sub.add_parser("deny", help="deny a pending payment")
    p.add_argument("request_id")
    p.add_argument("reason", nargs="*")
    p.set_defaults(func=_deny)

    p = sub.add_parser("cancel", help="cancel a payment stuck in checkout")
    p.add_argument("request_id", nargs="?")
    p.set_defaults(func=_cancel)

    p = sub.add_parser("init", help="create tables and seed demo data")
    p.add_argument("--reset", action="store_true", help="drop all tables first (destroys data)")
    p.set_defaults(func=_init)
    p = sub.add_parser("serve", help="run the API server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=_serve)
    sub.add_parser("stop", help="stop the running server").set_defaults(func=_stop)
    sub.add_parser("verify-audit", help="verify the audit hash chain").set_defaults(func=_verify)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
