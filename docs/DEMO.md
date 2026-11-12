# Live demo: Claude Code buys a keyboard through PACT

A step-by-step script for a manual run with the **PayPal Sandbox** (about 3 minutes). Nothing here moves real
money: PACT refuses non-sandbox PayPal URLs. For a dry run without PayPal, set `PACT_PAYMENT_PROVIDER=simulation`;
the checkout page then says "SIMULATED CHECKOUT (not PayPal)".

The automated version of this flow is `tests/test_e2e.py` (`uv run pytest tests/test_e2e.py`).

## 0. Before you start

- `uv sync` and a `.env` copied from `.env.example` with `PACT_ADMIN_TOKEN` and, for PayPal,
  `PACT_PAYMENT_PROVIDER=paypal`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` (a Sandbox **business** app).
  Keep these off screen.
- A PayPal Sandbox **personal** (buyer) account to pay with. Keep its login to yourself; this document has no credentials.
- The Claude Code CLI (`claude`) installed, for the MCP registration.

## 1. Start PACT

```bash
uv run pact demo
```

This checks `.env` (including a live PayPal OAuth call), seeds the catalogue, policies and agents, writes the agent
keys to `.pact/agent_keys.json`, creates the shopper workspace `~/pact-shopper`, registers the `pact` MCP server
there and starts the server on http://localhost:8000. It refuses to start a second server (`uv run pact stop` stops it).

In another terminal, confirm everything is healthy:

```bash
uv run pact doctor        # failed checks print the fix next to them
```

Open the two UIs in a browser:

- Admin dashboard: http://localhost:8000/#/admin (sign in with `PACT_ADMIN_TOKEN`)
- Merchant Lab storefront: http://localhost:8000/#/shop

## 2. Ask Claude Code to shop

```bash
cd ~/pact-shopper && claude
```

> Find the best mechanical keyboard under $100 and purchase it.

Claude searches TechMart, picks the Atlas TKL ($79.00, rating 4.6, the best-rated under $100), adds it to its cart,
checks out and calls `pact_request_authorization`. The result is **PENDING_APPROVAL** (reason
`USER_APPROVAL_THRESHOLD`: $79 is above the $50 approval threshold) and the amount is already **reserved** against
the shared budget. Claude says it is waiting and calls `pact_wait_for_status`. It cannot approve anything itself.

## 3. Approve as the human

Either in the dashboard (Approvals, http://localhost:8000/#/admin/approvals) or in a terminal in the PACT repo:

```bash
uv run pact approve        # shows merchant, items, amount and policy reasons, then asks y/N
```

Both approve the **exact request hash that was displayed**: if the order changed in between, the approval fails.
Claude wakes up on its own with the single-use grant token (shown to it once), calls `pact_execute_action` and
returns a PayPal checkout link. (`uv run pact approvals` lists what is waiting; `uv run pact deny <id> [reason]` refuses.)

## 4. The buyer step (PayPal Sandbox)

Open the link Claude gives you. Log in with your **personal** Sandbox buyer account and press Pay. PayPal redirects
to PACT's `/payments/return`, PACT captures the payment, checks the captured amount, settles the budget and marks
the Merchant Lab order paid. Claude, still waiting, reports **EXECUTED** with the capture id.

If PayPal shows a card form instead of a login, the email is not a Sandbox personal account. If you abandon
checkout, `uv run pact cancel` lists and cancels stuck payments (it completes the request instead if PayPal
already captured).

## 5. Show the evidence

In the dashboard:

- Overview: the budget bars; the keyboard moved from reserved to settled.
- The request's detail page: state EXECUTED, the decision with reason codes, grant issued and consumed, the capture.
- Audit: every step (request created, approved, grant issued, execution, capture, settlement). Use the verify action
  (or `uv run pact verify-audit`) to show the hash chain is intact.
- Storefront: the order shows as paid in its order status page.

## 6. The replay-rejection moment

Back in Claude Code:

> Pay for that order again with the same grant.

The grant was consumed by the first execution. Presenting it again is rejected with **409 GRANT_ALREADY_USED** and
PACT records a `REPLAY_REJECTED` audit event (filter the audit view by that event type). No second charge occurs and
the audit chain still verifies. Claude must report the failure as returned and not work around it.

## 7. A human buys on the storefront

At http://localhost:8000/#/shop add a keyboard to the cart and check out. The website acts as the `ag_storefront`
agent, so it goes through the same policy, approval, grant and PayPal path: a $79 purchase shows
**waiting for approval**, you approve it (step 3), and the page then offers the PayPal checkout link.
Orders created by agents cannot be paid from the website.

## 8. Multi-agent shared budget

The shopping, research and procurement agents share one organisation budget ($500/day in the seeded policy), even
though each respects its own cap. In a terminal in the PACT repo:

```bash
export PACT_ADMIN_TOKEN=...      # same value as in .env (only used to read the budget table)
uv run python examples/multi_agent_demo.py --yes
```

The script refuses to run without `--yes` because it creates real authorization requests. It uses each agent's own
key from `.pact/agent_keys.json`, has the shopping and procurement agents request purchases concurrently ($203 and
$285, all PENDING_APPROVAL and reserved), then the research agent asks for a $45 dataset that is within its own $150
cap and is **DENIED with `BUDGET_EXCEEDED`** by the global budget. It prints each decision and the budget table,
then denies the pending requests it created so the reservations are released (`--keep` leaves them in place so you
can inspect them in the dashboard). Keys and tokens are never printed.

Run it before step 2 or after the keyboard purchase has settled: leftover reservations or spend in the same day
change the numbers.

## Reset

```bash
uv run pact stop
uv run pact init --reset   # destroys the database and keys file, then reseeds; run `uv run pact demo` afterwards
```

## What this demonstrates (spec section 19)

MCP connection, shared budgets, deterministic policy, human approval bound to the exact order, a restricted
single-use grant, a real PayPal Sandbox transaction, rejected replays, and a complete audit trail.
