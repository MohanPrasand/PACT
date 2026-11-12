# Using PACT from Claude Code

PACT's MCP server gives Claude Code governed shopping and payment tools. The MCP process holds only one
agent API key: it has no database access, no PayPal credentials and no ability to approve anything.

## 1. One command

```bash
uv sync
cp .env.example .env     # set PACT_ADMIN_TOKEN; for PayPal: PAYPAL_CLIENT_ID/SECRET + PACT_PAYMENT_PROVIDER=paypal
uv run pact demo
```

`pact demo` checks the configuration (including a live PayPal OAuth call), seeds the catalogue, policies and
agents, creates a separate **shopper workspace** at `~/pact-shopper` (with a `CLAUDE.md` for the agent),
registers the `pact` MCP server there (Claude Code local scope), and starts the server. It refuses to start a
second server; `pact stop` stops it and `pact doctor` diagnoses problems.

The shopper workspace keeps the agent away from PACT's repository and `.env`: the agent should hold only its
agent key, never the admin token.

## 2. Manual registration (optional)

```bash
claude mcp add pact \
  -e PACT_API_URL=http://localhost:8000 \
  -e PACT_AGENT_KEY=<ag_shopping key from .pact/agent_keys.json> \
  -- uv run --directory /absolute/path/to/PACT pact-mcp
```

Keep the agent key out of version control.

## 3. Try it

> Find the best mechanical keyboard under $100 and buy it.

What happens:
1. Claude searches (`merchant_search_products`), adds the keyboard to its cart and checks out (`merchant_checkout_cart`).
2. `pact_request_authorization` → **PENDING_APPROVAL** ($79 is above the $50 approval threshold).
   Claude tells you and waits (`pact_wait_for_status`).
3. You approve the exact transaction: `uv run pact approve` in the PACT repo. Claude cannot do this itself.
4. Claude resumes on its own with the single-use grant, calls `pact_execute_action` and gives you a PayPal Sandbox
   checkout link. Pay with a sandbox **personal** account (not the business account).
5. PACT captures, verifies the amount, settles the budget and marks the order paid. Claude, still waiting,
   reports **EXECUTED** without being asked.
6. Ask Claude to pay again with the same grant: PACT rejects it (`GRANT_ALREADY_USED`) and audits the replay.

Until the dashboard (#14) lands, approve from a terminal with `pact approve` (it shows exactly what you are approving). The raw API calls are:

```bash
curl -s -H "Authorization: Bearer $PACT_ADMIN_TOKEN" localhost:8000/admin/authorizations?state=PENDING_APPROVAL
curl -s -X POST -H "Authorization: Bearer $PACT_ADMIN_TOKEN" -H 'content-type: application/json' \
  -d '{"request_hash":"<request_hash from the listing>"}' localhost:8000/admin/authorizations/<request_id>/approve
```

## Tools

| Merchant | PACT |
|---|---|
| `merchant_search_products`, `merchant_get_product` | `pact_request_authorization`, `pact_evaluate_policy` |
| `merchant_add_to_cart`, `merchant_view_cart`, `merchant_checkout_cart` | `pact_get_authorization_status`, `pact_execute_action` |
| `merchant_create_order`, `merchant_get_order`, `merchant_list_transactions` | `pact_wait_for_status`, `pact_revoke_grant`, `pact_get_audit_log`, `pact_get_budget` |

Tool names use `_` because Claude tool names cannot contain `.` (spec §10 lists them as `merchant.*` / `pact.*`).
