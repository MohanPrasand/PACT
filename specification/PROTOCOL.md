# PACT Protocol Reference

Wire-level reference for the PACT REST API and MCP tools. The code is the source of truth (`pact_core/`, `pact_server/app.py`, `pact_mcp/server.py`); this document is checked by reading it against them. PACT is a **sandbox reference implementation**, not a certified financial security product.

## 1. Conventions

- **Money:** integer minor units (`amount_minor`, cents for USD) everywhere in the core and API. MCP tools accept decimal strings (`"79.00"`) and convert at the edge. Prices and totals always come from the merchant.
- **Time:** ISO-8601 UTC strings.
- **Identifiers:** prefixed random ids (`auth_…`, `ord_…`, `gr_…`).
- **Errors:** every error is `{"error": {"code": "<STABLE_CODE>", "message": "…", "details": {…}}}` with the HTTP status of the error class. Validation failures are `422 INVALID_REQUEST` with `details.errors[{loc,msg}]`.
- **Request hash:** `sha256(canonical_json{agent_id, user_id, merchant_id, order_id, action_type, amount_minor, currency})`. Changing any bound field changes the hash.

## 2. Authentication boundaries

| Caller | Credential | Routes | Can approve? |
|---|---|---|---|
| Agent | `Authorization: Bearer <agent api key>` | `/v1/*` | no |
| Admin / dashboard | `Authorization: Bearer <PACT_ADMIN_TOKEN>` | `/admin/*` | yes |
| Buyer browser, provider redirects | none | `/payments/return`, `/payments/cancel`, `/sim/*` | no (only act on already-authorized payments) |
| Merchant Lab / storefront | none (sandbox demo simplification) | `/merchant/*` | no |

An agent key on an admin route is rejected (403). Agent keys are stored only as SHA-256 hashes.

## 3. Authorization lifecycle

```
CREATED → EVALUATING → DENIED | PENDING_APPROVAL | APPROVED
PENDING_APPROVAL → APPROVED | DENIED | EXPIRED | REVOKED
APPROVED         → GRANTED | EXPIRED | REVOKED
GRANTED          → EXECUTING | EXPIRED | REVOKED
EXECUTING        → EXECUTED | FAILED | UNKNOWN
UNKNOWN          → EXECUTING | EXECUTED | FAILED      (only via provider reconciliation)
Terminal: DENIED, EXECUTED, FAILED, EXPIRED, REVOKED
```

Transitions are compare-and-set (`UPDATE … WHERE state = :src`); a lost race raises `CONCURRENT_STATE_CHANGE`. Budget is reserved in `PENDING_APPROVAL`, `APPROVED`, `GRANTED`, `EXECUTING` and `UNKNOWN`, settled on `EXECUTED` and released on every other terminal state.

## 4. Agent API (`/v1`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/me` | Caller identity (`agent_id`, `user_id`, `name`) |
| POST | `/v1/authorizations` | Request authorization (201) |
| POST | `/v1/authorizations/evaluate` | Dry-run policy evaluation; reserves nothing |
| GET | `/v1/authorizations/{id}` | Status; delivers the grant token once after approval |
| POST | `/v1/authorizations/{id}/execute` | Execute with the grant token |
| POST | `/v1/authorizations/{id}/revoke` | Revoke own request |
| GET | `/v1/audit` | Own audit events |
| GET | `/v1/budget` | Budget scopes that apply to the agent |

### 4.1 Request

```json
{ "merchant_id": "m_techmart", "order_id": "ord_…", "amount_minor": 7900,
  "currency": "USD", "action_type": "PURCHASE", "description": "keyboard" }
```

The order is re-read from the merchant: the amount, merchant and currency actually evaluated and bound are the merchant's, not the agent's claim (`ORDER_MISMATCH` otherwise).

### 4.2 Decision (`decision` field of a request)

```json
{ "decision": "ALLOW | DENY | REQUIRE_APPROVAL",
  "reason_codes": ["USER_APPROVAL_THRESHOLD"],
  "findings": [{ "source": "system|org|user|merchant|agent|budget", "policy_id": "user:u_demo",
                 "rule": "…", "effect": "…", "code": "…", "message": "…",
                 "limit_minor": 5000, "observed_minor": 7900 }],
  "policies_evaluated": [{ "id": "org:default", "version": 1 }],
  "applicable_limits": { "…": 0 }, "approval_required": true,
  "approval_reasons": ["…"], "policy_version": "<hash>" }
```

Strictest effect wins: any DENY → DENY; else any REQUIRE_APPROVAL → REQUIRE_APPROVAL; else ALLOW. Any evaluation error or missing policy → DENY (`POLICY_EVALUATION_ERROR`). Approval can never override a DENY or a failed budget reservation.

### 4.3 Request view

`request_id, state, agent_id, user_id, merchant_id, order_id, action_type, amount_minor, amount, currency, description, request_hash, decision, policy_version, approval_expires_at, decided_by, decided_at, decision_note, grant {grant_id, expires_at, consumed, revoked} | null, execution {provider, provider_label, provider_order_id, provider_status, capture_id, approval_url, failure_reason}, created_at, updated_at, next_action`. `grant_token` appears **once**, on the first status response after approval (or in the create response when the request is automatically allowed), and is never stored in plaintext.

### 4.4 Execute

```json
{ "grant_token": "…", "merchant_id": "…", "order_id": "…", "amount_minor": 7900,
  "currency": "USD", "idempotency_key": "optional, 8-128 chars" }
```

Validation chain: caller owns the request → grant exists, unused, unexpired, unrevoked → bound agent, merchant, order, amount, currency match → request hash intact → live merchant order unchanged since authorization → request is `GRANTED` → active budget reservation → grant consumed atomically (single use) → provider order created. A repeat with the same token and idempotency key returns the existing execution; any other reuse is `GRANT_ALREADY_USED` and audited as `REPLAY_REJECTED`.

A provider timeout moves the request to `UNKNOWN`. It is reconciled against the provider and never retried blindly.

## 5. Admin API (`/admin`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/admin/overview` | Budgets, agents, requests by state, recent requests, audit chain status |
| GET | `/admin/authorizations[?state=]` | List requests |
| GET | `/admin/authorizations/{id}` | Detail incl. order snapshot, reservations, audit |
| POST | `/admin/authorizations/{id}/approve` | Body `{request_hash, note?}`; the hash must equal the displayed one |
| POST | `/admin/authorizations/{id}/deny` | Body `{reason?}` |
| POST | `/admin/authorizations/{id}/revoke` | Revoke any request |
| POST | `/admin/authorizations/{id}/cancel` | Abandon a payment stuck in checkout |
| POST | `/admin/authorizations/{id}/reconcile` | Re-check with the provider |
| GET / PUT | `/admin/policies`, `/admin/policies/{id}` | Read / update (`{rules, expected_version}`; stale version → 409 `POLICY_VERSION_CONFLICT`, invalid rules → 400 `POLICY_INVALID`) |
| GET | `/admin/agents`, `/admin/budgets` | Agents with spend; budget scopes |
| GET | `/admin/audit[?request_id=&event_type=]`, `/admin/audit/verify` | Events; hash-chain verification |

## 6. Storefront and provider routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/merchant/merchants`, `/merchant/products`, `/merchant/products/{id}` | Catalogue (`q`, `category`, `max_price`, `merchant_id`, `sort`) |
| POST / GET / PATCH | `/merchant/orders`, `/merchant/orders/{id}` | Create / read / modify items |
| GET | `/merchant/orders/{id}/status` | Order + merchant-safe authorization view (no token, hash or keys) |
| POST | `/merchant/orders/{id}/checkout` | Human web checkout through PACT as `ag_storefront`; idempotent and resumable; `{retry: true}` starts a new attempt after a failed one |
| GET / POST / DELETE | `/merchant/carts/{cart_id}…`, `/merchant/carts/{cart_id}/checkout` | Cart; checkout creates an order |
| GET | `/payments/return`, `/payments/cancel` | Provider redirect targets (capture / abandon) |
| GET / POST | `/sim/checkout/{order_id}[/approve|/cancel]` | Simulated checkout (simulation provider only, labelled "not PayPal") |

## 7. State and reason vocabulary

**Policy reason codes:** `WITHIN_POLICY`, `ACTION_NOT_ALLOWED`, `CURRENCY_NOT_SUPPORTED`, `INVALID_AMOUNT`, `ORDER_NOT_FOUND`, `ORDER_MISMATCH`, `ORDER_NOT_PAYABLE`, `MERCHANT_UNKNOWN`, `MERCHANT_NOT_APPROVED`, `MERCHANT_AGENT_NOT_ALLOWED`, `MERCHANT_CATEGORY_NOT_ALLOWED`, `MERCHANT_TRANSACTION_LIMIT`, `MERCHANT_APPROVAL_REQUIRED`, `SYSTEM_TRANSACTION_LIMIT`, `ORG_TRANSACTION_LIMIT`, `USER_TRANSACTION_LIMIT`, `USER_APPROVAL_THRESHOLD`, `AGENT_INACTIVE`, `AGENT_POLICY_DISABLED`, `AGENT_MERCHANT_NOT_ALLOWED`, `AGENT_TRANSACTION_LIMIT`, `AGENT_APPROVAL_REQUIRED`, `BUDGET_EXCEEDED`, `DUPLICATE_ORDER_AUTHORIZATION`, `POSSIBLE_DUPLICATE_PAYMENT`, `POLICY_EVALUATION_ERROR`.

**Grant / execution errors:** `GRANT_NOT_FOUND`, `GRANT_INVALID`, `GRANT_ALREADY_USED`, `GRANT_EXPIRED`, `GRANT_REVOKED`, `GRANT_AGENT_MISMATCH`, `GRANT_BINDING_MISMATCH`, `REQUEST_HASH_MISMATCH`, `ORDER_MODIFIED_SINCE_REQUEST`, `INVALID_STATE`, `RESERVATION_MISSING`, `CONCURRENT_STATE_CHANGE`, `NOT_PENDING_APPROVAL`, `APPROVAL_HASH_MISMATCH`, `APPROVAL_EXPIRED`.

**Audit event types:** `REQUEST_CREATED`, `POLICY_EVALUATED`, `BUDGET_RESERVED`, `BUDGET_RESERVATION_FAILED`, `APPROVAL_REQUESTED`, `APPROVED`, `DENIED`, `GRANT_ISSUED`, `GRANT_DELIVERED`, `GRANT_CONSUMED`, `GRANT_REVOKED`, `GRANT_EXPIRED`, `EXECUTION_REJECTED`, `REPLAY_REJECTED`, `PROVIDER_ORDER_CREATED`, `PROVIDER_TIMEOUT`, `PAYMENT_CAPTURED`, `EXECUTION_FAILED`, `RECONCILED`, `POLICY_UPDATED`. Each event stores `prev_hash` and `hash = sha256(prev_hash + canonical{seq, ts, event_type, actor, request_id, data})`; `/admin/audit/verify` recomputes the chain and reports the first break.

## 8. MCP tools (agent-facing)

`merchant_search_products`, `merchant_get_product`, `merchant_add_to_cart`, `merchant_view_cart`, `merchant_checkout_cart`, `merchant_create_order`, `merchant_get_order`, `merchant_list_transactions`, `pact_request_authorization`, `pact_evaluate_policy`, `pact_get_authorization_status`, `pact_wait_for_status`, `pact_execute_action`, `pact_revoke_grant`, `pact_get_audit_log`, `pact_get_budget`.

The MCP server is a thin REST client holding one agent key. **No tool can approve, deny, edit policy or read secrets.** Errors are returned as data (`{"error": {"code", "message"}}`) so the model reports the exact code. `pact_wait_for_status` returns when a human acts (grant delivered, denial, EXECUTED/FAILED) or after a timeout (max 300 s).

## 9. Policy documents

Strictly validated JSON per scope (`system`, `org`, `user`, `merchant`, `agent`); unknown fields, negative limits and type coercion are rejected. Schemas and the demo configuration: [`policies/README.md`](policies/README.md).
