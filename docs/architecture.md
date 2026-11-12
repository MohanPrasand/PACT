# PACT Architecture

This document describes how PACT Core is built and why. Sections marked *(planned)* describe components that are designed but not yet merged into `dev`; their issues are linked.

## 1. Layers and trust boundaries

| Layer | Holds | Can approve? | Can reach PayPal? |
|---|---|---|---|
| AI agent (Claude Code) | nothing | no | no |
| PACT MCP server *(planned, #13)* | one agent API key | no | no |
| PACT REST API *(planned, #12)* | agent auth, admin auth | admin token only | no |
| **PACT Core** | policies, budgets, requests, grants, audit | via `approve()` behind admin auth | only through the execution gateway |
| Execution gateway *(planned, #10)* | provider adapters | no | yes, the **only** caller |
| PayPal adapter *(planned, #11)* | client id/secret, OAuth token (in memory) | no | yes |

Rules that follow from this:
- **No tool approves:** there is no MCP tool and no agent-authenticated route that approves, denies or edits policy.
- **No secrets leave the server:** PayPal secrets and access tokens never leave the server process. They are never returned by the API or MCP, and never written to logs or audit events.
- **Core is interface-independent:** PACT Core doesn't import MCP, FastAPI, Merchant Lab or any payment provider. It depends on interfaces (`OrderSource`, `PaymentAdapter`), so REST, MCP and future SDKs share one implementation of every rule.

## 2. Module map (merged)

| Module | Responsibility |
|---|---|
| `pact_core/config.py` | `Settings` from env/`.env`; fail-fast validation (PayPal creds in paypal mode, sandbox-only base URL, ISO currency); secrets as `SecretStr`, inputs hidden in errors |
| `pact_core/db.py` | `Database`, `transaction()`, naive-UTC `utcnow()`, `Clock` type, SQLite configuration |
| `pact_core/models.py` | `Agent`, `Policy`, `AuthorizationRequest`, `Grant`, `BudgetCounter`, `Reservation`, `BudgetLedgerEntry`, `AuditEvent`, `AuditHead` |
| `pact_core/state_machine.py` | `TRANSITIONS`, `TERMINAL`, `RESERVING`, `can_transition`, `transition` |
| `pact_core/policy/rules.py` | Strict typed schemas per policy scope; `validate_rules` |
| `pact_core/policy/engine.py` | `evaluate(EvaluationInput, PolicySnapshot) -> PolicyDecision`, `budget_scopes` |
| `pact_core/audit.py` | `append`, `verify_chain`, `to_dict`, `EventType` |
| `pact_core/crypto.py` | canonical JSON, SHA-256, ids/secrets, `request_hash`, constant-time compare |
| `pact_core/money.py` | decimal string ⇄ integer minor units |
| `pact_core/errors.py` | `PactError` hierarchy with stable codes and HTTP status |

## 3. Data conventions

- **Money:** always integer **minor units** (cents). Conversion happens only at the edges (`pact_core/money.py`).
- **Time:** naive UTC, taken from an injected `Clock`. Tests use a controllable `FixedClock`. The policy engine never reads a clock.
- **Identifiers:** prefixed random ids (`auth_…`, `gr_…`, `ord_…`). Grant tokens use 32 random bytes and are stored only as SHA-256 hashes.
- **Request hash:** `sha256(canonical{agent_id, user_id, merchant_id, order_id, action_type, amount_minor, currency})`. Changing any field gives a different hash, and that is what binds an approval to one transaction.

## 4. Concurrency model

Shared budgets are only meaningful if concurrent agents can't both spend the same headroom.

1. **One transaction per write path.** Every mutation runs inside exactly one `Database.transaction()`, and transactions are never nested.
2. **SQLite serialises writers.** Every transaction starts with `BEGIN IMMEDIATE` and uses WAL mode and a busy timeout, so read-modify-write cannot lose updates. This is tested with 8 threads × 15 increments.
3. **Invariants are conditional updates.** Atomic invariants use `UPDATE … WHERE <invariant holds>` and check the row count. Examples:
   - A budget reservation runs `UPDATE counters SET reserved = reserved + :amt WHERE limit - reserved - settled + refunded >= :amt`. In a test with 20 contenders and room for exactly 5, exactly 5 win.
   - A state transition runs `UPDATE requests SET state = :dst WHERE id = :id AND state = :src`. Under contention exactly one wins; the others get `CONCURRENT_STATE_CHANGE`.

   Because this pattern relies only on standard SQL, it stays correct on PostgreSQL.
4. **No network I/O inside a transaction.** Provider and Merchant Lab calls happen before or after a transaction, never during one. That avoids holding the write lock across the network, and same-thread deadlocks.

## 5. Authorization state machine

- **Allowed edges:** `TRANSITIONS` is a read-only mapping, and every state has an entry. Terminal states (`DENIED`, `EXECUTED`, `FAILED`, `EXPIRED`, `REVOKED`) have no outgoing edges.
- **`transition()` is compare-and-set.** Unknown state names and edges not in `TRANSITIONS` raise `INVALID_STATE_TRANSITION`. If the stored row is no longer in the expected source state, the in-memory object is stale and the call raises `CONCURRENT_STATE_CHANGE`.
- **Reserving states:** `RESERVING` (`PENDING_APPROVAL`, `APPROVED`, `GRANTED`, `EXECUTING`, `UNKNOWN`) lists the states in which a request holds a budget reservation.
- **`UNKNOWN`** is entered when a provider call's outcome is ambiguous (timeout, 5xx, dropped connection). It is left only through reconciliation with the provider.
- **Caller requirement:** set `state=State.CREATED` explicitly on a new request before transitioning it in the same transaction. The column default is applied only when the row is flushed.

## 6. Policy evaluation

```
EvaluationInput (facts)  ─┐
                          ├─> evaluate() ─> PolicyDecision
PolicySnapshot (docs)    ─┘
```

- **Facts are authoritative.** The order amount, merchant and status come from Merchant Lab, never from the agent. If the agent's claimed values differ, the request is denied with `ORDER_MISMATCH`.
- **Snapshot:** each slot holds `(policy_id, version, rules)`, for the system, org, user, merchant and agent scopes. `rules` may be a typed model or a raw dict as stored in the database; raw dicts are validated during evaluation.
- **Composition:** every applicable rule emits a `Finding(source, policy_id, rule, effect, code, message, limit_minor, observed_minor)`. If any finding is DENY, the decision is DENY. Otherwise, if any finding requires approval, the decision is REQUIRE_APPROVAL. Otherwise it is ALLOW, with reason `WITHIN_POLICY`.
- **Fail closed:**
  - A missing system, org, user or agent document produces `<SCOPE>_POLICY_MISSING`.
  - A malformed document produces `<SCOPE>_POLICY_INVALID`.
  - Any exception produces `POLICY_EVALUATION_ERROR`.

  Each of these results in DENY. The merchant document is optional; without one, the merchant imposes no restrictions of its own.
- **Integrity checks always run,** even without a system document: agent active, merchant known, order exists and matches, order payable, no duplicate authorization.
- **Budgets:** the engine checks budget headroom (`org:global`, `user:<id>`, `agent:<id>`) so the decision can explain itself. The budget engine then enforces those limits atomically.
- **Output:**
  - `decision` and `reason_codes`
  - `findings`
  - `policies_evaluated` (each id with its version)
  - `applicable_limits`, including `effective_max_transaction`
  - `approval_required` and `approval_reasons`
  - `policy_version`: a content hash over every document's scope, id, version and rules

Reason codes and rule fields: [`specification/policies/README.md`](../specification/policies/README.md).

## 7. Audit log

- **Hash chain.** Each event stores `hash = sha256(prev_hash || canonical{seq, ts, event_type, actor, request_id, data})`. Events have contiguous `seq` numbers starting at 1, and the first event links to a genesis hash of 64 zeros.
- **Atomic with the change.** Events are appended in the same transaction as the state change they describe, so a decision and its audit record commit or roll back together.
- **Serialised appends.** A single `AuditHead` row (last seq, last hash) is created idempotently. It is locked `FOR UPDATE` on PostgreSQL; on SQLite, `BEGIN IMMEDIATE` serialises appends instead.
- **Canonicalised before hashing:**
  - Timestamps are naive UTC with fixed microsecond precision.
  - `actor`, `event_type` and `request_id` are converted to strings.
  - Datetimes and decimals inside `data` are converted to strings.
  - NaN and Infinity are rejected.
- **Verification:** `verify_chain()` returns `{valid, events_checked, broken_at_seq, reason, head_seq, head_hash}`. The possible reasons are `hash mismatch`, `sequence gap`, `head mismatch` and `missing head`.
- **Export:** `to_dict()` emits the exact hashed timestamp string, so exported events can be re-verified outside PACT.
- **Known limitation:** deleting the last event and also rolling back the head can't be detected from inside the database. Record `head_hash` somewhere external to detect truncation.

## 8. Planned components

| Component | Design summary | Issue |
|---|---|---|
| Budget engine | Counters per scope and UTC day, plus an append-only ledger (`RESERVE`, `RELEASE`, `SETTLE`, `REFUND`) and per-request reservations. A reservation covers every scope a request draws from, inside one SAVEPOINT. Invariant: `available + reserved + (settled − refunded) = limit`. | #6 |
| Grant manager | Opaque token, stored only as a hash. Bound to the agent, user, merchant, order, amount, currency and request hash. Expires. Consumed atomically by a conditional update. | #7 |
| Authorization service | Request, evaluate, approve (with the hash the human saw), deny, revoke, expire. Single-use grant delivery to the requesting agent. | #8 |
| Merchant Lab | Catalogue, cart and orders, behind the `OrderSource` port. Prices are set server-side, and an order is marked captured only after PACT verifies the payment. | #9 |
| Execution gateway | Ten validation steps (spec §7.6), then an atomic grant consume, then the provider call outside the transaction. Timeouts move the request to `UNKNOWN` until reconciled. Captured amount and currency are verified before settlement. | #10 |
| PayPal adapter | Orders API v2 with OAuth, idempotent via `PayPal-Request-Id`, sandbox only. | #11 |
| REST API, MCP server | A thin REST layer. The MCP server is a thin HTTP client holding only an agent key; tool names use `_`. | #12, #13 |
| Dashboard and storefront | One React/Vite/Tailwind app: `#/admin` (approvals, policies, audit) and `#/shop` (catalogue, live cart, order-status timeline). | #14, #22–#25 |
