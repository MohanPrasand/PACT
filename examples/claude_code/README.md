# Using PACT from Claude Code

PACT's MCP server gives Claude Code governed shopping and payment tools. The MCP process holds only one
agent API key: it has no database access, no PayPal credentials and no ability to approve anything.

## 1. Start PACT

```bash
uv sync
cp .env.example .env          # set PACT_ADMIN_TOKEN; for PayPal also PAYPAL_CLIENT_ID/SECRET + PACT_PAYMENT_PROVIDER=paypal
uv run pact init              # seeds catalogue, policies, demo agents; prints agent keys once (.pact/agent_keys.json)
uv run pact serve             # http://localhost:8000  (API docs at /docs)
```

## 2. Register the MCP server

Either copy `mcp.json.example` to `.mcp.json` in your project and fill in the path and key, or run:

```bash
claude mcp add pact \
  -e PACT_API_URL=http://localhost:8000 \
  -e PACT_AGENT_KEY=<ag_shopping key> \
  -- uv run --directory /absolute/path/to/PACT pact-mcp
```

Keep the agent key out of version control (`.mcp.json` with a real key should not be committed).

## 3. Try it

> Find the best mechanical keyboard under $100 and buy it.

What happens:
1. Claude searches (`merchant_search_products`), adds the keyboard to its cart and checks out (`merchant_checkout_cart`).
2. `pact_request_authorization` → **PENDING_APPROVAL** ($79 is above the $50 approval threshold).
3. You approve the exact transaction (admin API / dashboard). Claude cannot do this itself.
4. `pact_get_authorization_status` returns the single-use grant; `pact_execute_action` returns a PayPal Sandbox
   approval URL. Open it and pay with a sandbox *personal* account.
5. PACT captures, verifies the amount, settles the budget and marks the order paid; Claude sees **EXECUTED**.
6. Ask Claude to pay again with the same grant: PACT rejects it (`GRANT_ALREADY_USED`) and audits the replay.

Until the dashboard (#14) lands, approve with:

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
| `merchant_create_order`, `merchant_get_order`, `merchant_list_transactions` | `pact_revoke_grant`, `pact_get_audit_log`, `pact_get_budget` |

Tool names use `_` because Claude tool names cannot contain `.` (spec §10 lists them as `merchant.*` / `pact.*`).
