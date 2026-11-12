# Phase 2 — Governance Core: Parallel Implementation Plan

Tracking: epic #18 · Integration branch: `feat/phase-2-governance` · Spec: §7.3, §7.5, §8, §9, §12, §14.2–14.3

## Outcome — ✅ completed 2026-11-12

| Wave | Issue | PR | Result |
|---|---|---|---|
| A | #6 Budget engine | #31 | 8 bugs fixed — incl. stale ORM reads (reported 100 reserved vs real 300), negative refunds, non-atomic release/settle on PostgreSQL (57 tests) |
| A | #7 Grant manager | #29 | New module exactly per contract; atomic consume, check-order precedence (75 tests) |
| A | #9 Merchant Lab | #30 | Seed catalogue; strict payment-status transitions; 7 bugs fixed (140 tests) |
| A | #11 Adapters | #32 | Strict sandbox URL check, 401 re-auth, secret redaction, order-id path-injection guard; simulation thread-safety (193 tests) |
| B | #8 + #10 Authorization service, execution gateway, `PactCore` | #33 | Full governed flow; 50 tests incl. concurrency, replay, timeouts after side effects |

**1151 tests** passing. Verified live against PayPal Sandbox through `PactCore` up to buyer approval (order created, reconciled on status poll).

**Carried into Phase 5 (#12/#13)**
- REST: `/payments/return|cancel` → `PactCore.handle_provider_return|cancel`; simulated checkout page using `SimulationAdapter.buyer_approve|buyer_cancel`; admin auth; agent keys must get 403 on admin routes
- `pact init` → `merchant_lab.seed.seed_catalogue`, `pact_core.seed.seed_policies|seed_agents`
- Background expiry sweeper → `PactCore.expire_stale()` every `PACT_EXPIRY_SWEEP_SECONDS`
- Merchant Lab items accept only `product_id` + `quantity`

## Goal
Build the governance building blocks that the authorization service and execution gateway compose:

| Wave | Issues | Mode |
|---|---|---|
| 0 | Integration branch, drafts imported, shared interfaces frozen | sequential (main session) |
| **A** | **#6** budget engine · **#7** grant manager · **#9** Merchant Lab · **#11** PayPal + simulation adapters | **4 parallel agents** |
| B | #8 authorization service → #10 execution gateway | sequential (they compose everything from Wave A) |
| C | Integrate, full suite, PR → `dev` | main session |

#9 (officially Phase 3) and #11 (officially Phase 4) are pulled forward because they depend only on Phase 1 and the frozen ports. That shortens the path to the first end-to-end PayPal Sandbox purchase.

## Branch model
```
dev
 └─ feat/phase-2-governance                 ← integration branch
     ├─ feat/phase-2/6-budget                ← agent A
     ├─ feat/phase-2/7-grants                ← agent B
     ├─ feat/phase-2/9-merchant-lab          ← agent C
     └─ feat/phase-2/11-payment-adapters     ← agent D
```
Agent worktrees may start from `main`. Every brief requires verifying the base commit and resetting onto `origin/feat/phase-2-governance` (lesson from Phase 1).

## Wave 0 (done in this commit)
- Drafts imported from `feat/core-foundation`: `pact_core/budget.py`, `pact_core/ports.py`, `merchant_lab/{models,service}.py`, `adapters/{paypal,simulation}/adapter.py`, `examples/paypal_sandbox_check.py`
- `tests/conftest.py`: the `db` fixture now also creates Merchant Lab tables
- **Frozen for Wave A:** `pact_core/models.py`, `pact_core/db.py`, `pact_core/ports.py`, `pact_core/crypto.py`, `pact_core/money.py`, `pact_core/errors.py`, `pact_core/policy/**`, `pact_core/state_machine.py`, `pact_core/audit.py`, `tests/conftest.py`

## File ownership (Wave A)

| Agent | Issue | May edit / create |
|---|---|---|
| A | #6 Budget | `pact_core/budget.py`, `tests/test_budget.py` |
| B | #7 Grants | `pact_core/grants.py` (new), `tests/test_grants.py` |
| C | #9 Merchant Lab | `merchant_lab/**` (incl. new `merchant_lab/seed.py`), `tests/test_merchant_lab.py` |
| D | #11 Adapters | `adapters/**`, `examples/paypal_sandbox_check.py`, `tests/test_paypal_adapter.py`, `tests/test_simulation_adapter.py` |

If an agent needs a change in a frozen file, it **stops and reports** instead of editing.

## Interface contracts
Wave B depends on these names. Keep them stable, or report any change.

### #6 `pact_core/budget.py` (draft exists)
- `BudgetEngine(clock)`
  - `.reserve(s, request_id, amount_minor, scopes)` raises `BudgetExhausted(scope_key)`
  - `.release(s, request_id) -> int`
  - `.settle(s, request_id, amount_minor=None) -> int`
  - `.refund(s, request_id, amount_minor) -> int`
  - `.usage(s, scope_key, limit_minor, period=None) -> ScopeUsage`
  - `.used(s, scope_key, period=None) -> int`
  - `.active_reservations(s, request_id)`
  - `.check_invariants(s) -> list[str]`
- `period_for(ts)`
- `scopes` come from `pact_core.policy.engine.budget_scopes(snapshot, agent_id, user_id)`.

### #7 `pact_core/grants.py` (new)
```python
@dataclass(frozen=True)
class GrantClaim:            # what the executing agent presents
    request_id: str
    token: str
    agent_id: str            # authenticated caller, not self-declared
    merchant_id: str
    order_id: str
    amount_minor: int
    currency: str

class GrantManager:
    def __init__(self, clock: Clock, ttl_seconds: int): ...
    def issue(self, s, req: AuthorizationRequest) -> tuple[Grant, str]:
        """Create the grant for an APPROVED request; returns (grant, plaintext token). Only sha256(token) is stored."""
    def validate(self, s, claim: GrantClaim, *, current_request_hash: str) -> Grant:
        """All checks, no mutation. Raises PactError with one of the codes below."""
    def consume(self, s, grant: Grant) -> None:
        """Atomic conditional UPDATE (consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now); GRANT_ALREADY_USED on race."""
    def revoke(self, s, request_id: str, actor: str) -> bool: ...
    def find_expired_unconsumed(self, s) -> list[Grant]: ...
```
Error codes, in the order they are checked:

| Order | Code |
|---|---|
| 1 | `GRANT_NOT_FOUND` |
| 2 | `GRANT_INVALID` (token hash mismatch, constant-time compare) |
| 3 | `GRANT_AGENT_MISMATCH` |
| 4 | `GRANT_REVOKED` |
| 5 | `GRANT_ALREADY_USED` |
| 6 | `GRANT_EXPIRED` |
| 7 | `GRANT_BINDING_MISMATCH` (`details["field"]` ∈ merchant, order, amount, currency) |
| 8 | `REQUEST_HASH_MISMATCH` (grant hash ≠ request row hash) |
| 9 | `ORDER_MODIFIED` (`current_request_hash`, recomputed by the caller from the live Merchant Lab order, ≠ grant hash) |

The grant manager does **not** write audit events or change request state. The services in #8 and #10 do that, so audit and state changes stay in one place. For example, `REPLAY_REJECTED` is written by #10.

### #9 `merchant_lab` (draft exists)
- `MerchantLab(db)`: catalogue search/get, cart (`cart_add`, `cart_remove`, `cart_view`, `order_from_cart`), `create_order`, `update_order_items`, `get_order`, `list_transactions`, `list_merchants`
- `MerchantLabOrderSource(lab)`: implements `pact_core.ports.OrderSource`
- **New** `merchant_lab/seed.py`: `seed_catalogue(db)`, idempotent; the merchants and products are listed in issue #9
- REST routes are **not** in scope; they belong to #12.

### #11 `adapters` (draft exists)
- `PayPalAdapter(client_id, client_secret, *, base_url, timeout, brand_name, transport=None)` and `SimulationAdapter(public_url)`, both implementing `pact_core.ports.PaymentAdapter`
- Simulation-only helpers: `buyer_approve(order_id)`, `lookup(order_id)` and fault injection with `inject(operation, kind, after_side_effect=False)`
- `/payments/return|cancel` routes and the simulated checkout page are **not** in scope; they belong to #12.

## Per-agent definition of done
1. The issue's acceptance criteria (`gh issue view <n> -R MohanPrasand/PACT`) are met. Items assigned above to #8, #10 or #12 are listed in the PR as deferred.
2. `uv run pytest` passes for the whole suite.
3. Commits are `type(scope): subject` with `Refs #<n>` and the repo `Co-Authored-By` trailer.
4. The PR goes **into `feat/phase-2-governance`**, titled `[P2] #<n> …`. Its body maps each acceptance criterion to the tests that cover it.
5. Report back: PR URL, test count, bugs fixed, deviations, open questions.

## Shared rules
- Money is integer minor units; time is naive UTC from the injected `Clock`.
- One transaction per write path. No network I/O inside a transaction. Atomicity comes from conditional `UPDATE … WHERE` plus a row-count check.
- Never print, log or commit secrets. Tests never read `.env` and never call real PayPal; use `httpx.MockTransport`.
- The live sandbox check script runs only manually.
- Scope guard (spec §4.3, §18): nothing beyond the issue.

## Wave B preview
- **#8 authorization service:** `request_authorization` → evaluate → reserve → DENIED / PENDING_APPROVAL / GRANTED. Also covers approve (with the hash the human saw), deny, revoke, `expire_stale` and single-use grant delivery. Must set `state=State.CREATED` explicitly when creating a request.
- **#10 execution gateway:** the validation chain using `GrantManager.validate` plus the budget reservation check, then `consume`, then the adapter call outside the transaction. Covers `UNKNOWN` and reconciliation, capture verification, settlement, the `REPLAY_REJECTED` audit event and spec §14.4 tests (using the simulation adapter).
