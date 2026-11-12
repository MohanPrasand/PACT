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

## 8. Checkout lifecycle after execution starts

`pact_core/gateway.py` is the only code that calls a payment adapter. `execute()` consumes the grant, moves the request `GRANTED → EXECUTING` in one transaction and creates the provider order outside it. From then on the buyer is on the provider's checkout page and the request holds its budget reservation until one of the paths below ends it.

All of this is implemented on the `fix/paypal-gateway` branch (#38–#42). Run against the live PayPal Sandbox so far: order creation, early return, cancel link, explicit cancel, polling, order-not-found and checkout timeout. Buyer approval, capture, decline and the unknown-outcome paths are tested with the simulation adapter and a mocked PayPal API only.

### Paths

| Event | What the gateway does | Request afterwards | Budget reservation |
|---|---|---|---|
| Buyer approves and returns (`GET /payments/return`) | Captures with key `pact-<request>-capture`, then checks captured amount and currency against the authorization | `EXECUTED` | settled |
| Capture is still pending at the provider | Notes `CAPTURE_PENDING`; a later poll looks again | `EXECUTING` | held |
| Order is not approved yet (`ORDER_NOT_APPROVED`, `PAYER_ACTION_REQUIRED`) | Notes the status | `EXECUTING` | held |
| Buyer uses the cancel link (`GET /payments/cancel`) | Asks the provider for the order. A completed order is finalized. Otherwise sets `provider_status = BUYER_CANCELLED` and audits `CHECKOUT_CANCELLED_BY_BUYER` once. The buyer can retry at the same `approval_url` | `EXECUTING` | held |
| Buyer cancels explicitly (`POST /payments/abandon`) | Asks the provider first; a completed order is finalized instead | `FAILED / BUYER_CANCELLED` | released |
| Operator runs `pact cancel` | Same provider check. If the provider cannot be asked, the call is refused (`CANNOT_VERIFY_PROVIDER`) and nothing changes | `FAILED / ABANDONED_CHECKOUT` | released |
| Payment method declined (`INSTRUMENT_DECLINED`) | Sets `provider_status = INSTRUMENT_DECLINED`. The buyer returns to the same `approval_url` and picks another method; the next capture uses the next idempotency key | `EXECUTING` | held |
| Provider rejects the capture with any other error | Looks at the order once (see below) | `FAILED / CAPTURE_REJECTED:<code>` | released |
| Capture response reports the capture as `DECLINED` or `FAILED` | Fails the request | `FAILED / CAPTURE_DECLINED` or `CAPTURE_FAILED` | released |
| Provider says the order does not exist (`RESOURCE_NOT_FOUND`, PayPal's 404 `INVALID_RESOURCE_ID`) | No order means no capture, so the request fails. Exception: a capture call with an unknown outcome (see below) | `FAILED / PROVIDER_ORDER_NOT_FOUND` | released |
| Provider reports the order `VOIDED` | Fails the request | `FAILED / PROVIDER_ORDER_VOIDED` | released |
| Checkout older than `PACT_CHECKOUT_TTL_SECONDS` | The sweeper asks the provider. A completed order is finalized and an approved one is captured; otherwise it audits `CHECKOUT_TIMED_OUT` and fails the request | `FAILED / CHECKOUT_TIMEOUT` | released |
| Captured amount or currency differs from the authorization | Audits `CAPTURE_MISMATCH` and holds the request for manual review | `UNKNOWN / CAPTURE_AMOUNT_MISMATCH` | held |
| Create or capture call times out, or the capture status is unclear | See "Unknown outcomes" | `UNKNOWN` | held |

### Cancel and decline are not terminal

- **Cancel link:** PayPal sends the buyer to the cancel URL with an unauthenticated GET, and the order stays payable. So the GET only records `BUYER_CANCELLED` on a request that stays `EXECUTING`. Ending the payment needs the explicit POST, which runs the provider-checked path.
- **Decline:** `INSTRUMENT_DECLINED` means the buyer has to choose another funding source for the same order. The request stays `EXECUTING`.
- **Status is kept:** while the provider still reports `PAYER_ACTION_REQUIRED`, reconciliation does not overwrite a `BUYER_CANCELLED` or `INSTRUMENT_DECLINED` status. It is cleared when the order becomes `APPROVED`.
- **Agents:** the MCP instructions tell the agent to hand out the same `approval_url` again and keep waiting, and not to start a new purchase or authorization unless the request is terminal.

### Capture idempotency key

- The first capture uses `pact-<request>-capture`. After each definitive `INSTRUMENT_DECLINED` the key advances to `pact-<request>-capture-<n>`, where `n` is the number of such declines in the audit log. Without this, a provider that replays the stored response for a reused key would return the old decline forever.
- The key never advances after a timeout. A capture with an unknown outcome is repeated, if at all, with the same key, so the provider cannot capture twice.

### Capture rejected

A capture error that is neither "not approved yet" nor a decline goes to `_after_capture_rejected`, which calls `get_order` exactly once and never captures again from there:

- **Order completed or has a capture** (for example `ORDER_ALREADY_CAPTURED`): finalize as a normal capture.
- **Lookup fails:** note the error code and stay `EXECUTING`; the next poll tries again.
- **Retryable error** (rate limit, transient auth): stay `EXECUTING`.
- **Otherwise:** `FAILED / CAPTURE_REJECTED:<provider code>`.

`_capture` never calls reconciliation, and reconciliation calls `_capture` at most once, so there is no retry loop.

### Checkout timeout

- **When:** the expiry sweeper (`PACT_EXPIRY_SWEEP_SECONDS`) picks up `EXECUTING` requests whose grant was consumed more than `PACT_CHECKOUT_TTL_SECONDS` ago (default 3600; `0` disables).
- **Provider first:** it runs the same provider-checked path as an explicit cancel, with the provider call outside the database transaction. A payment the buyer completed in the meantime is finalized, not failed.
- **Left alone:** `UNKNOWN` requests are never timed out. If the provider cannot be reached, the request waits for the next sweep.

### Unknown outcomes and reconciliation

- **Entering `UNKNOWN`:** a timeout, 5xx or dropped connection on create or capture, an unclear capture status, or an amount mismatch. The request keeps its reservation.
- **Leaving it:** only through `reconcile()`, which asks the provider what happened. An unknown create is retried with the same idempotency key, so the provider returns the original order. A completed order is finalized, an approved one is captured, and one still waiting for the buyer goes back to `EXECUTING`.
- **Order not found while a capture is in doubt:** the two answers conflict, so the request is held and the 404 is audited once (see "Capture in doubt").
- **Polling:** a status read of a live request triggers reconciliation. Provider lookups are limited to one per request every 2 seconds (inside that interval the stored view is returned), and a `RECONCILED` audit event is written only when the provider status, capture status or request state changed.

### Capture in doubt

This is the rule the rest of the section depends on. A request is *in doubt* when money may have moved or may be about to:

| Marker | Set when |
|---|---|
| `failure_reason = CAPTURE_IN_PROGRESS` | Committed to the database before every capture call, cleared by that call's answer |
| `failure_reason = PROVIDER_TIMEOUT_ON_CAPTURE` | A capture call timed out, returned 5xx or dropped |
| `failure_reason = CAPTURE_AMOUNT_MISMATCH` / `CAPTURE_STATUS_UNCLEAR` | The provider showed a capture PACT could not settle |
| `provider_status = CAPTURE_PENDING` | The provider reports the capture as pending |

While a request is in doubt:

- **Nothing releases the reservation on indirect evidence.** An order lookup can lag behind a capture, so "still approved", "not approved", "order not found", a buyer or operator cancel and the checkout timeout all leave the request as it is. `_fail()` checks this itself, in the same transaction as the release, so a cancel cannot overtake a capture that has already been sent.
- **Only the capture's own answer decides.** An open capture call is repeated with the same idempotency key (by a status poll, `reconcile`, or a cancel attempt). The provider then answers for that call: captured (finalize), or definitively not (`ORDER_NOT_APPROVED`, a decline, a rejection), which clears the marker and returns an `UNKNOWN` request to `EXECUTING`.
- **Cancel attempts are refused** with `CANNOT_VERIFY_PROVIDER` if that does not settle it.
- **Manual exit:** `pact cancel <id> --force` (admin API `?force=true`) closes a payment PACT cannot verify, for example when the provider no longer answers for the order. It releases the reservation without provider evidence, so the operator must check the PayPal account first. It is audited as `CANCEL_FORCED` and is refused while a capture call is in flight.

### Polls after a decline

A status poll does not retry a declined capture immediately, because the order usually still carries the declined funding source. The buyer's return always retries. If the buyer chose another source and never came back, polls retry by themselves at most 3 times, at least 60 seconds apart; after that the checkout timeout makes one last attempt.

### Cancel link while the provider is unreachable

Nothing was learned, so nothing changes: the request stays `EXECUTING` and no audit event is written.

### Budget release

A reservation is released in exactly one place, `_fail()`, in the same transaction as the move to `FAILED`. It is settled in exactly one place, `_finalize()`, after the captured amount and currency have been checked. `_fail()` refuses to act on a request that is in doubt unless the caller holds the provider's own verdict on the capture.

| Path to `FAILED` | Evidence that no money moved |
|---|---|
| Explicit cancel, operator cancel, checkout timeout | No capture is in doubt, and `get_order` shows no capture or the provider says the order does not exist |
| Order not found | The provider has no such order, and no capture is in doubt |
| Capture rejected | The provider refused the capture with a non-retryable error, and a following `get_order` shows no capture |
| Capture `DECLINED` / `FAILED` | The provider reports that verdict on the capture itself |
| Order `VOIDED` | The provider reports that state, and no capture is in doubt |
| `pact cancel --force` | None from the provider: an operator's audited decision |

When that evidence is missing, nothing is released: the request stays `EXECUTING` or `UNKNOWN`, both of which are reserving states (section 5).

## 9. Planned components

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
