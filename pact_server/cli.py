"""``pact`` command-line interface.

    pact init [--reset]     create tables, seed catalogue, policies and demo agents (prints agent keys once)
    pact serve [--port N]   run the API (+ dashboard if built) with the expiry sweeper
    pact verify-audit       verify the tamper-evident audit chain
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

KEYS_FILE = Path(".pact") / "agent_keys.json"


def _init(args) -> int:
    import merchant_lab.models  # noqa: F401
    from merchant_lab.seed import seed_catalogue
    from pact_core.config import get_settings
    from pact_core.db import Database
    from pact_core.seed import seed_agents, seed_policies

    settings = get_settings()
    db = Database(settings.database_url)
    if args.reset:
        db.drop_all()
        if KEYS_FILE.exists():
            KEYS_FILE.unlink()
    db.create_all()
    catalogue = seed_catalogue(db)
    policies = seed_policies(db)
    keys = seed_agents(db)

    print(f"Database:  {settings.database_url}")
    print(f"Catalogue: {catalogue['merchants_total']} merchants, {catalogue['products_total']} products")
    print(f"Policies:  {len(policies)} loaded" if policies else "Policies:  already present (kept)")
    if keys:
        KEYS_FILE.parent.mkdir(exist_ok=True)
        existing = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}
        existing.update(keys)
        fd = os.open(KEYS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"Agents:    {len(keys)} created; API keys written to {KEYS_FILE} (mode 600). Only hashes are stored in the DB.")
        for agent_id, key in keys.items():
            print(f"  {agent_id:16s} {key}")
    else:
        print(f"Agents:    already present; keys are in {KEYS_FILE} (they cannot be recovered from the DB)")
    if not settings.admin_token:
        print("WARNING:   PACT_ADMIN_TOKEN is not set; the dashboard/approval API will be disabled.", file=sys.stderr)
    print(f"Provider:  {settings.payment_provider}  ·  currency {settings.currency}")
    return 0


def _serve(args) -> int:
    import uvicorn

    uvicorn.run("pact_server.app:create_app", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def _verify(_args) -> int:
    from pact_core import audit
    from pact_core.config import get_settings
    from pact_core.db import Database

    db = Database(get_settings().database_url)
    with db.transaction() as s:
        result = audit.verify_chain(s)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pact", description="PACT — Policy and Authorization Control Tower")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create tables and seed demo data")
    p_init.add_argument("--reset", action="store_true", help="drop all tables first (destroys data)")
    p_init.set_defaults(func=_init)
    p_serve = sub.add_parser("serve", help="run the API server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=_serve)
    p_verify = sub.add_parser("verify-audit", help="verify the audit hash chain")
    p_verify.set_defaults(func=_verify)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
