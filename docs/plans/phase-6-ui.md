# Phase 6 — Dashboard and Merchant Lab Storefront: Implementation Plan

Tracking: epic #18 · Branch: `feat/phase-6-ui` · Issues: #22, #14, #23, #24, #25 · Spec: §8.1, §11, §13

Phases 1–2 (core) and 5 (REST API, MCP, CLI) are merged. Phase 6 adds the two UIs. One frontend project (`dashboard/`) hosts both, served by FastAPI from `dashboard/dist`.

## Work breakdown

| Wave | Work | Issue | Owns (no overlap) |
|---|---|---|---|
| 0 | Web app scaffold: Vite + React + TS + Tailwind, hash router, two API clients, money/status/poll primitives, shells, CI `npm run build` | #22 | `dashboard/` config, `src/lib`, `src/shared`, `src/App.tsx`, `.github/workflows/ci.yml` |
| A1 | Admin dashboard: Overview, Policies, Approval Centre, Audit Explorer, request drawer + Reconcile | #14 | `dashboard/src/admin/**` |
| A2 | Backend for storefront: `GET /merchant/orders/{id}/status` (merchant-safe), `ag_storefront` identity + policy seed, `POST /merchant/orders/{id}/checkout` | #24, #25 | `pact_server/`, `pact_core/seed.py`, `tests/` |
| A3 | Storefront UI: catalogue and product pages, live cart, orders list and order status stepper | #23, #24, #25 | `dashboard/src/shop/**` |
| B | Integrate, `npm run build`, serve from FastAPI, full pytest, browser check of the demo flow | all | main session |

Wave 0 is sequential because every UI task needs its primitives and routes. A1, A2 and A3 touch disjoint paths and run in parallel. A3 codes against the endpoint contract below and can use the already-merged `/merchant/*` routes for catalogue and cart.

## Contract for the new endpoints (A2 implements, A3 consumes)

`GET /merchant/orders/{order_id}/status` (no auth, sandbox demo simplification)
```json
{
  "order": { "...": "same shape as GET /merchant/orders/{id}" },
  "payment_status": "CREATED|PENDING|APPROVED|CAPTURED|FAILED|CANCELLED",
  "authorization": null | {
    "request_id": "auth_…", "state": "…", "decision": "ALLOW|REQUIRE_APPROVAL|DENY|null",
    "reason_codes": ["…"], "approval_pending": true,
    "provider": "paypal|simulation", "provider_label": "PayPal Sandbox|Simulation (not PayPal)",
    "provider_order_id": "…|null", "capture_id": "…|null", "checkout_url": "…|null",
    "created_at": "…", "updated_at": "…"
  }
}
```
Never includes grant tokens, policy internals beyond reason codes, agent keys or PayPal credentials.

`POST /merchant/orders/{order_id}/checkout` → requests PACT authorization as `ag_storefront`; executes at once when GRANTED and returns `{state, request_id, checkout_url?, reason_codes?}`. PENDING_APPROVAL returns the state only. The storefront can never approve.

## Status (2026-11-12)

| Item | State |
|---|---|
| Plan | done (this file) |
| A2 backend (`ag_storefront`, `/merchant/orders/{id}/status`, `/merchant/orders/{id}/checkout`) | done, 9 tests in `tests/test_storefront.py`, full suite green |
| Wave 0 scaffold source (`dashboard/`) | written, **not yet installed or built**: the sandbox blocked `npm install` because the configured private npm registry does not resolve here |
| A1 dashboard, A3 storefront UI | not started (need a working `npm install` to typecheck/build) |

Notes from implementation:
- The order row is linked to an authorization only once payment starts, so the status view finds the request with `PactCore.latest_request_for_order`.
- Existing databases need `pact init` once to create the `ag_storefront` agent and policy.
- Web checkout is unauthenticated in the sandbox demo; it can only start a PACT-governed request (approval still required above the user threshold), and refuses orders owned by another agent.

## Rules for every task
- Grant tokens are never rendered or stored client-side. The storefront API client has no admin token and never calls `/admin/*`.
- Prices, totals and statuses come from the API. No client-side arithmetic on money except display formatting from minor units.
- No external runtime assets (fonts, images, CDNs). Must work offline.
- Provider label is exact: "PayPal Sandbox" vs "Simulation (not PayPal)".

## Definition of done per task
1. Issue acceptance criteria met; anything deferred is listed in the PR.
2. `npm run build` (typecheck + build) passes; `uv run pytest` passes.
3. No edits outside the task's owned paths without noting why.
