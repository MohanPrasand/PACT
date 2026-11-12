# PayPal Gateway Fixes — Parallel Implementation Plan

Tracking: #38 #39 #40 #41 #42 #43 · Integration branch: `fix/paypal-gateway` (worktree `.claude/worktrees/fix-paypal-gateway`, cut from `origin/dev` @ c504e07) · Target: PR → `dev`

## Why

Eight PayPal Sandbox purchases were attempted; none reached buyer approval. Seven ended `FAILED / BUYER_CANCELLED` and one is stuck `EXECUTING`. The analysis found two confirmed gateway bugs and four gaps:

| Issue | Type | One line |
|---|---|---|
| #38 | bug, reproduced | PayPal's 404 code (`INVALID_RESOURCE_ID`) is not recognised; request stuck `EXECUTING`, `pact cancel` fails |
| #39 | bug, reproduced | `_capture` ↔ `_reconcile_with` recurse without bound on a hard capture rejection |
| #40 | bug | Buyer cancel link is terminal, on an unauthenticated GET |
| #41 | bug | No recovery after `INSTRUMENT_DECLINED`; capture idempotency key reused |
| #42 | bug | `EXECUTING` never times out; each status poll calls PayPal and writes `RECONCILED` |
| #43 | enhancement | No `landing_page`; `pact doctor` only checks OAuth; README overstates live verification |

## Prerequisite (Wave 0 decides)

`dev` does not contain two payment commits that sit only on the local `feat/phase-6-ui` branch:

- `4c20d35 feat(admin): cancel a payment stuck in checkout (pact cancel)` — adds `ExecutionGateway.abandon`, `/admin/authorizations/{id}/cancel`, `pact cancel`
- `780442d fix(payments): clearer PayPal Sandbox buyer hint`

#38, #40 and #42 all build on `abandon`. **Recommended:** cherry-pick both commits onto `fix/paypal-gateway` in Wave 0. They touch only payment code, and identical patches merge cleanly when `feat/phase-6-ui` later lands on `dev`. If either does not apply cleanly, stop and ask.

## Waves

| Wave | Work | Mode |
|---|---|---|
| 0 | Cherry-pick prerequisite, baseline test run, freeze the contracts below | sequential (main session) |
| **A** | **A1** gateway correctness (#38, #39) · **A2** adapter + config (#43) · **A3** doctor + docs (#43) | **3 parallel agents** |
| **B** | **B1** checkout lifecycle in the gateway (#40, #41, #42) · **B2** return/cancel pages and routes (#40, #41) · **B3** MCP wording + README/architecture | **3 parallel agents** |
| C | Integrate, full suite, live Sandbox run, PR → `dev` | main session + 1 review agent |

Wave B starts only after A1 is merged into the integration branch, because B1 edits the same functions in `pact_core/gateway.py`. A2 and A3 may still be running when Wave B starts; they share no files with it.

## Branch model

```
dev
 └─ fix/paypal-gateway                    ← integration branch (this worktree)
     ├─ fix/paypal-gateway-a1-correctness  ← agent A1
     ├─ fix/paypal-gateway-a2-adapter      ← agent A2
     ├─ fix/paypal-gateway-a3-doctor       ← agent A3
     ├─ fix/paypal-gateway-b1-lifecycle    ← agent B1 (cut after A1 merges)
     ├─ fix/paypal-gateway-b2-pages        ← agent B2 (cut after A1 merges)
     └─ fix/paypal-gateway-b3-docs         ← agent B3
```

Each agent runs with `isolation: "worktree"`, commits to its own branch, and never pushes. The main session merges each branch into `fix/paypal-gateway` with `--no-ff` and runs the full suite after every merge.

## File ownership (no file has two owners in the same wave)

| Agent | Owns | Must not touch |
|---|---|---|
| A1 | `pact_core/gateway.py`, `tests/test_gateway.py` | adapters, server |
| A2 | `adapters/paypal/adapter.py`, `pact_core/config.py`, `pact_server/app.py::build_adapters` only, `.env.example`, `tests/test_paypal_adapter.py`, `tests/test_config.py` | gateway, ops, cli |
| A3 | `pact_server/ops.py`, `pact_server/cli.py`, `examples/paypal_sandbox_check.py`, `tests/test_convenience.py`, new `docs/paypal-sandbox-setup.md` | adapter, gateway, `app.py` |
| B1 | `pact_core/gateway.py`, `pact_core/runtime.py`, `pact_core/authorization.py`, `pact_core/core.py`, `pact_core/audit.py`, `pact_core/config.py` (one new setting), `tests/test_gateway.py`, `tests/test_authorization.py`, `tests/test_audit.py` | `pact_server/`, adapters |
| B2 | `pact_server/app.py` (routes and pages, not `build_adapters`), `tests/test_api.py` | `pact_core/` |
| B3 | `pact_mcp/server.py` (instruction text only), `README.md`, `docs/architecture.md`, `tests/test_mcp.py` | everything else |

`pact_core/config.py` is owned by A2 in Wave A and B1 in Wave B, so there is no same-wave overlap. If A2 is still open when B1 starts, B1 adds its setting at the end of the settings block to keep the merge trivial.

## Frozen contracts (set in Wave 0; agents code against these, not against each other)

### Provider "order not found" (#38)
- `pact_core/gateway.py`: `NOT_FOUND = {"RESOURCE_NOT_FOUND", "INVALID_RESOURCE_ID"}`.
- The PayPal adapter's error mapping is unchanged (its tests pin `INVALID_RESOURCE_ID` for 404).

### Capture rejection (#39)
- `_capture` never calls `_reconcile_with`. `_reconcile_with` may call `_capture` once. There is no other cycle.
- New `failure_reason`: `CAPTURE_REJECTED:<provider code>`.

### Checkout statuses visible to pages and agents (#40, #41)
`view["execution"]["provider_status"]` gains two PACT-set values while the request stays `EXECUTING`:

| `provider_status` | Meaning | Buyer can retry at `approval_url` |
|---|---|---|
| `BUYER_CANCELLED` | buyer hit the cancel link; order still payable | yes |
| `INSTRUMENT_DECLINED` | capture declined; buyer must pick another funding source | yes |

`failure_reason` values: `BUYER_CANCELLED` (explicit cancel only), `CHECKOUT_TIMEOUT`, `CAPTURE_REJECTED:<code>`, `PROVIDER_ORDER_NOT_FOUND`.

### Gateway methods (B1 implements, B2 calls through `PactCore`)
```python
PactCore.handle_provider_cancel(provider_order_id) -> view   # non-terminal: verify, note BUYER_CANCELLED, stay EXECUTING
PactCore.buyer_abandon(provider_order_id) -> view            # terminal: provider-checked, FAILED / BUYER_CANCELLED
PactCore.expire_stale() -> list[str]                         # now also times out EXECUTING checkouts
```

### Routes (B2)
```
GET  /payments/cancel?token=<provider order id>     no terminal effect; page with "Try again" + "Cancel this payment"
POST /payments/abandon   (form field: token)        terminal; calls PactCore.buyer_abandon
```

### Settings
```
PAYPAL_LANDING_PAGE          LOGIN | GUEST_CHECKOUT | NO_PREFERENCE     default LOGIN        (A2)
PACT_CHECKOUT_TTL_SECONDS    int, 0 disables                            default 3600         (B1)
```

### Audit event types (register in `pact_core/audit.py`; B1)
`CHECKOUT_CANCELLED_BY_BUYER`, `CHECKOUT_TIMED_OUT`. `RECONCILED` is written only on change.

## Wave 0 — main session

1. `git cherry-pick 4c20d35 780442d` on `fix/paypal-gateway`.
2. `uv sync --locked && uv run pytest -q`; record the baseline count.
3. Confirm the contracts above still match the code after the cherry-pick; adjust this file if not.
4. Commit this plan.

## Wave A

### A1 — Gateway correctness (#38, #39)

**#38**
1. Add `NOT_FOUND`; use it in `_reconcile_with` in place of the `== "RESOURCE_NOT_FOUND"` comparison.
2. In `abandon` and `handle_provider_cancel`, a `ProviderError` whose code is in `NOT_FOUND` means "no order, so no capture": continue to the fail path instead of `CANNOT_VERIFY_PROVIDER` / `CANCEL_UNVERIFIED`.
3. Guard: if the request's `failure_reason` is `PROVIDER_TIMEOUT_ON_CAPTURE` (a capture call had an unknown outcome), a 404 does not auto-fail. Leave it `UNKNOWN` and audit the 404.

**#39**
1. Replace the `return self._reconcile_with(...)` in `_capture`'s `ProviderError` branch with a new `_after_capture_rejected(request_id, adapter, poid, exc)`:
   - `get_order` once. `ProviderTimeout` or `ProviderError` here: `_note_status(exc.code)` and return the view (the next poll retries; bounded).
   - order `COMPLETED`: `_finalize`.
   - `exc.retryable`: `_note_status(exc.code)`, stay `EXECUTING`.
   - otherwise `_fail(request_id, f"CAPTURE_REJECTED:{exc.code}")`.
2. `NOT_READY` and `BUYER_RETRY` handling is unchanged.

**Tests (`tests/test_gateway.py`)**
- PayPal's real 404 body through `PayPalAdapter` with `httpx.MockTransport`: reconcile ends `FAILED / PROVIDER_ORDER_NOT_FOUND`, reservation released.
- `abandon` succeeds when the provider order is gone.
- 404 after an unknown capture outcome stays `UNKNOWN`.
- Persistent `PAYER_CANNOT_PAY`: `FAILED / CAPTURE_REJECTED:PAYER_CANNOT_PAY`, exactly one `capture_order` and one `get_order` call.
- `ORDER_ALREADY_CAPTURED` finalizes to `EXECUTED`.
- 429 on capture stays `EXECUTING`.
- Budget invariants and `verify_audit()` hold in each.

### A2 — Adapter and config (#43)

1. `Settings.paypal_landing_page: Literal["LOGIN", "GUEST_CHECKOUT", "NO_PREFERENCE"] = "LOGIN"` (`PAYPAL_LANDING_PAGE`).
2. `PayPalAdapter.__init__(..., landing_page="LOGIN")`; validate; send as `experience_context.landing_page`.
3. `build_adapters` passes the setting.
4. Add the payee's `merchant_id` and `email_address` to `ProviderOrder.raw["payee"]` when PayPal returns them (non-secret; A3's doctor check reads it).
5. `.env.example`: document the new variable.

**Tests:** request body carries `landing_page`; invalid value rejected at config and adapter level; `raw["payee"]` parsed; nothing secret enters `raw`.

### A3 — Doctor and Sandbox docs (#43)

1. `pact doctor` in PayPal mode, after the OAuth check:
   - create a minimal order in `PACT_CURRENCY` via `PayPalAdapter.create_order`; report `PROVIDER_REJECTED` codes such as `CURRENCY_NOT_SUPPORTED` with a fix hint;
   - print the payee business account from `raw.get("payee")` (tolerate its absence until A2 merges) with "buyers must log in with a different, personal Sandbox account";
   - warning-only note when the account cannot take direct card payments.
2. `examples/paypal_sandbox_check.py`: print the payee the same way.
3. New `docs/paypal-sandbox-setup.md`: business + personal Sandbox accounts in a country pair that can transact, REST app created under that business account, where to find the personal account's password, card testing limits, and the statement that PACT never stores buyer credentials.

**Tests (`tests/test_convenience.py`):** doctor order check passes, fails with a provider code, and tolerates a missing payee, all with a mock transport and no network.

## Wave B (after A1 is merged)

### B1 — Checkout lifecycle (#40, #41, #42)

**#40 non-terminal cancel**
1. `handle_provider_cancel`: provider check as today; `COMPLETED` finalizes; otherwise set `provider_status = "BUYER_CANCELLED"`, audit `CHECKOUT_CANCELLED_BY_BUYER` once per transition, stay `EXECUTING`.
2. New `buyer_abandon(provider_order_id)`: resolves the request and runs the provider-checked terminal path with reason `BUYER_CANCELLED` (share the body with `abandon`).
3. `runtime.next_action`: for `BUYER_CANCELLED` say the buyer backed out and can retry at the same `approval_url`; add it to the status list that shows the URL.
4. `_reconcile_with`: when PayPal still reports `PAYER_ACTION_REQUIRED`, do not overwrite a PACT-set `BUYER_CANCELLED` / `INSTRUMENT_DECLINED` status. Clear it when the order becomes `APPROVED`.

**#41 capture key**
1. Capture key becomes `pact-<request>-capture` for the first attempt and `pact-<request>-capture-<n>` afterwards, where `n` is the number of audited definitive rejections (`CAPTURE_NOT_COMPLETED` with status in `BUYER_RETRY`) for the request. No schema change.
2. The key never advances after `ProviderTimeout`.
3. Gate: do this only if the Wave C Sandbox check shows PayPal replays the stored decline for a reused `PayPal-Request-Id`. If PayPal re-attempts, keep the single key and close that part of #41 with the evidence. Implement behind a small helper so either outcome is a one-line change.

**#42 timeout and polling**
1. `Settings.checkout_ttl_seconds` (`PACT_CHECKOUT_TTL_SECONDS`, default 3600, 0 disables), threaded through `PactContext`.
2. `expire_stale`: for `EXECUTING` requests whose grant was consumed more than the TTL ago, call the provider-checked abandon path with reason `CHECKOUT_TIMEOUT` and audit `CHECKOUT_TIMED_OUT`. Run provider calls outside the DB transaction. Never touch `UNKNOWN`. A provider that cannot be reached leaves the request for the next sweep.
3. `_reconcile_with`: write `RECONCILED` only when provider status, capture status or request state differs from what is stored.
4. Per-request minimum interval (2 s, in-memory, clock-injected) for provider lookups triggered by `get_status`; inside the interval return the stored view.

**Tests:** cancel then approve reaches `EXECUTED`; `buyer_abandon` fails and releases; decline → re-approve → second capture uses the next key; timeout → retry uses the same key; TTL expiry fails and releases, and finalizes a captured order; `UNKNOWN` untouched; 50 polls give one `RECONCILED` and a bounded call count; update the existing `BUYER_CANCELLED` assertion at `tests/test_gateway.py:147`.

### B2 — Return and cancel pages (#40, #41)

1. `GET /payments/cancel` renders from the non-terminal view: "Checkout cancelled — nothing was charged", a "Try again" link to `approval_url`, and a form posting to `/payments/abandon`.
2. `POST /payments/abandon` (form field `token`, same length limit) calls `core.buyer_abandon`.
3. `_result_page`:
   - `provider_status == "INSTRUMENT_DECLINED"`: explain, link "Choose another payment method" to `approval_url`;
   - `failure_reason` starting `CAPTURE_REJECTED:`: say PayPal refused the payment and no money moved;
   - keep the Sandbox login hint on the cancelled page, pointing to `docs/paypal-sandbox-setup.md`.
4. Only link to an `approval_url` that came from the stored view; escape everything.

Until B1 merges, B2 tests stub `PactCore.buyer_abandon` and the view fields from the frozen contract.

**Tests (`tests/test_api.py`):** GET cancel leaves state `EXECUTING` and shows both actions; POST abandon fails the request; declined page shows the link; update the assertion at `tests/test_api.py:144`; simulation `/sim/checkout/.../cancel` still works end to end.

### B3 — MCP wording and docs

1. `pact_mcp/server.py` instructions: a buyer who cancelled or was declined can retry at the same `approval_url`; do not start a new purchase unless the request is terminal.
2. README: new settings, corrected "verified live" claim (filled in with the Wave C result), link to the Sandbox setup guide.
3. `docs/architecture.md`: checkout lifecycle (cancel, decline, timeout, not-found).

## Wave C — integrate and verify (main session)

1. Merge order: A1 → A2 → A3 → B1 → B2 → B3, full suite after each.
2. Independent review agent on the combined diff, focused on: money safety (no path releases a reservation when a capture is possible), recursion and loop bounds, unauthenticated routes, audit chain validity.
3. Live Sandbox run (`PACT_PAYMENT_PROVIDER=paypal`):
   - `pact doctor` shows the payee and passes the order check;
   - the stuck request from the analysis resolves to `FAILED / PROVIDER_ORDER_NOT_FOUND` on the next status poll or `pact cancel`;
   - cancel link, then "Try again", then pay → `EXECUTED`;
   - forced decline with the `PayPal-Mock-Response` negative-testing header on capture, twice with the same `PayPal-Request-Id`, to settle the #41 gate;
   - one full login → approve → capture → `EXECUTED`; record it in the README.
4. PR `fix/paypal-gateway` → `dev`, closing #38–#42 and the completed parts of #43.

## Risks

| Risk | Mitigation |
|---|---|
| Treating 404 as terminal releases budget for an order captured under different credentials | Guard in A1 step 3; doctor prints the payee so a credential switch is visible |
| Timeout sweeper fails a payment the buyer is completing | Provider-checked path finalizes `COMPLETED` and captures `APPROVED` before failing; TTL default 1 hour |
| Advancing the capture key causes a double capture | Key advances only after a definitive 4xx; PayPal also refuses a second capture of the same order |
| Buyer login still fails after these fixes | That part is Sandbox account setup, not code; covered by the setup guide and doctor output |
| Wave B agents drift from the contract | Contracts are frozen in this file; B2 tests against stubs and is re-run against B1 at merge |

## Agent brief template

Each agent gets: the issue numbers, its section of this plan, its owned files, the frozen contracts, and these rules: stay inside owned files; `uv run pytest -q` must pass before each commit; no network in tests; never read or print `.env` values; commit to the agent branch and do not push; report changed files, new tests and any contract change it needed (do not make the change, report it).
