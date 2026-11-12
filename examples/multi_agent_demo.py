"""Multi-agent shared-budget demo (spec section 2).

The shopping, research and procurement agents each stay inside their own limits, yet together they would exceed
the organisation's daily budget. PACT's shared budget makes the last request lose, even though it is within its
own agent cap.

It talks to a RUNNING PACT server over REST with each agent's own key (from the keys file `pact init` / `pact demo`
wrote) and reads the budget table with the admin token from the environment. It creates real authorization
requests (they stay PENDING_APPROVAL and reserve budget), so it refuses to run without --yes.
Keys and tokens are never printed.

    export PACT_ADMIN_TOKEN=...            # same value as in PACT's .env
    uv run python examples/multi_agent_demo.py --yes

When it finishes it denies the pending requests it created (releasing their reservations) unless --keep is given.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pact_server import ops  # noqa: E402

# (agent, product id, quantity). Amounts follow the seeded demo policies: org budget $500/day; agent caps
# shopping $250, procurement $300, research $150.
PLAN: dict[str, list[tuple[str, int]]] = {
    "ag_shopping": [("prd_kb_titan_rgb", 1), ("prd_kb_forge_75", 1)],          # $139 + $64  = $203
    "ag_procurement": [("prd_monitor_27_qhd", 1), ("prd_hub_usbc_7", 4)],      # $189 + $96  = $285
}
FINAL = ("ag_research", "prd_ds_retail_prices", 1)                              # $45, within research's own cap


@dataclass
class Demo:
    api: str
    admin_token: str
    keys: dict[str, str]
    lock: threading.Lock = field(default_factory=threading.Lock)
    created: list[str] = field(default_factory=list)

    def say(self, line: str) -> None:
        with self.lock:
            print(line, flush=True)

    def agent(self, agent: str) -> httpx.Client:
        return httpx.Client(base_url=self.api, timeout=30,
                            headers={"Authorization": f"Bearer {self.keys[agent]}"})

    def request(self, agent: str, product_id: str, qty: int) -> dict:
        with self.agent(agent) as http:
            product = http.get("/merchant/products/" + product_id).json()
            order = http.post("/merchant/orders", json={"items": [{"product_id": product_id, "quantity": qty}],
                                                        "buyer_ref": agent}).raise_for_status().json()
            r = http.post("/v1/authorizations", json={
                "merchant_id": order["merchant_id"], "order_id": order["order_id"], "amount_minor": order["total_minor"],
                "currency": order["currency"], "description": f"{qty} x {product['name']} (multi-agent demo)"})
            r.raise_for_status()
            view = r.json()
        with self.lock:
            self.created.append(view["request_id"])
        codes = ",".join(view["decision"]["reason_codes"]) or "-"
        self.say(f"  {agent:<15} {usd(order['total_minor']):>9}  {qty} x {product['name']:<42} "
                 f"-> {view['state']:<17} [{codes}]")
        return view

    def run_agent(self, agent: str) -> None:
        for product_id, qty in PLAN[agent]:
            self.request(agent, product_id, qty)

    def budgets(self) -> list[dict]:
        r = httpx.get(f"{self.api}/admin/overview", headers={"Authorization": f"Bearer {self.admin_token}"}, timeout=30)
        r.raise_for_status()
        return r.json()["budgets"]

    def release(self) -> None:
        for rid in self.created:
            d = httpx.get(f"{self.api}/admin/authorizations/{rid}", headers={"Authorization": f"Bearer {self.admin_token}"})
            if d.status_code == 200 and d.json()["state"] == "PENDING_APPROVAL":
                httpx.post(f"{self.api}/admin/authorizations/{rid}/deny", timeout=30,
                           headers={"Authorization": f"Bearer {self.admin_token}"},
                           json={"reason": "multi-agent demo cleanup"})


def usd(minor: int) -> str:
    return f"${minor / 100:,.2f}"


def print_table(budgets: list[dict]) -> None:
    print(f"\n  {'scope':<24}{'limit':>10}{'reserved':>11}{'settled':>10}{'available':>11}")
    for b in sorted(budgets, key=lambda b: b["scope"]):
        print(f"  {b['scope']:<24}{usd(b['limit_minor']):>10}{usd(b['reserved_minor']):>11}"
              f"{usd(b['settled_minor']):>10}{usd(b['available_minor']):>11}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Three agents share one organisation budget (PACT multi-agent demo).")
    ap.add_argument("--yes", action="store_true", help="required: this creates real authorization requests")
    ap.add_argument("--api-url", default=os.environ.get("PACT_API_URL", "http://localhost:8000"))
    ap.add_argument("--keys-file", type=Path, default=None, help="agent keys JSON (default: the default database's)")
    ap.add_argument("--keep", action="store_true", help="leave the pending requests (and their reservations) in place")
    args = ap.parse_args(argv)

    if not args.yes:
        print("This demo creates real authorization requests on the PACT server at "
              f"{args.api_url} and reserves budget until they are denied.\nNothing was done. Re-run with --yes.")
        return 2
    admin_token = os.environ.get("PACT_ADMIN_TOKEN", "")
    if not admin_token:
        print("Set PACT_ADMIN_TOKEN in your environment (needed to read the budget table).", file=sys.stderr)
        return 1
    keys = ops.load_keys(args.keys_file or ops.keys_file(os.environ.get("PACT_DATABASE_URL", ops.DEFAULT_DATABASE_URL)))
    missing = [a for a in (*PLAN, FINAL[0]) if a not in keys]
    if missing:
        print(f"No agent keys for {', '.join(missing)}. Run `uv run pact init` first.", file=sys.stderr)
        return 1

    demo = Demo(args.api_url, admin_token, keys)
    try:
        health = httpx.get(f"{args.api_url}/health", timeout=10).json()
    except (httpx.HTTPError, ValueError):
        print(f"PACT is not reachable at {args.api_url}. Run `uv run pact doctor`.", file=sys.stderr)
        return 1
    print(f"PACT multi-agent demo  ·  provider {health.get('provider')}  ·  {args.api_url}")
    try:
        print("\nBudget before:")
        print_table(demo.budgets())

        print("\nShopping and procurement agents request purchases concurrently (each within its own cap):")
        threads = [threading.Thread(target=demo.run_agent, args=(a,)) for a in PLAN]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        print("\nThe research agent now asks for a dataset (within its own cap and merchant rules):")
        last = demo.request(*FINAL)

        print("\nBudget after (reservations count against the shared budget before anything is paid):")
        print_table(demo.budgets())
        if last["state"] == "DENIED" and "BUDGET_EXCEEDED" in last["decision"]["reason_codes"]:
            print("\nResult: the last request was DENIED by the global organisation budget "
                  "although it was within its own agent cap.")
            print(f"  {last['decision']['reason_codes']}")
            return 0
        print(f"\nResult: the last request was {last['state']}, not denied by the budget "
              "(the budget had more room than the demo plan assumes; check today's usage above).")
        return 0
    except httpx.HTTPStatusError as exc:
        print(f"Request failed: HTTP {exc.response.status_code} {exc.request.method} {exc.request.url.path}",
              file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            demo.release()
            print("\nPending demo requests denied; their reservations were released.")


if __name__ == "__main__":
    raise SystemExit(main())
