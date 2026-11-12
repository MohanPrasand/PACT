"""Human approval from the terminal (stand-in until the dashboard, #14, lands).

    uv run python examples/approvals.py                 # list pending requests with their policy findings
    uv run python examples/approvals.py approve <id>    # approve exactly what was listed (sends its request_hash)
    uv run python examples/approvals.py deny <id> [reason]

Reads PACT_ADMIN_TOKEN and PACT_PUBLIC_URL from the environment or .env. This is the *human* side of PACT:
agents never hold the admin token.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pact_core.config import get_settings  # noqa: E402


def main(argv: list[str]) -> int:
    s = get_settings()
    if not s.admin_token:
        print("PACT_ADMIN_TOKEN is not set")
        return 2
    api = httpx.Client(base_url=s.public_url, timeout=30,
                       headers={"Authorization": f"Bearer {s.admin_token.get_secret_value()}"})
    if not argv:
        pending = api.get("/admin/authorizations", params={"state": "PENDING_APPROVAL"}).json()["authorizations"]
        if not pending:
            print("No requests waiting for approval.")
        for p in pending:
            d = api.get(f"/admin/authorizations/{p['request_id']}").json()
            items = ", ".join(f"{i['quantity']}× {i['name']}" for i in (d.get("order_snapshot") or {}).get("items", []))
            print(f"\n{p['request_id']}  {p['amount']} {p['currency']}  agent={p['agent_id']}  "
                  f"merchant={d.get('merchant_name')} ({p['merchant_id']})")
            print(f"  order {p['order_id']}: {items}")
            for f in p["decision"]["findings"]:
                print(f"  [{f['effect']}] {f['code']}: {f['message']}")
            print(f"  approve: uv run python examples/approvals.py approve {p['request_id']}")
        return 0
    action, rid = argv[0], argv[1] if len(argv) > 1 else None
    if action not in ("approve", "deny") or not rid:
        print(__doc__)
        return 2
    if action == "approve":
        req = api.get(f"/admin/authorizations/{rid}").json()
        if "error" in req:
            print(req["error"]["message"])
            return 1
        r = api.post(f"/admin/authorizations/{rid}/approve", json={"request_hash": req["request_hash"],
                                                                    "note": "approved from terminal"})
    else:
        r = api.post(f"/admin/authorizations/{rid}/deny", json={"reason": " ".join(argv[2:]) or "denied from terminal"})
    body = r.json()
    print(body["error"]["code"] + ": " + body["error"]["message"] if "error" in body else f"{rid} -> {body['state']}")
    return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
