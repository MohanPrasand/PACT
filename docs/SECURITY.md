# PACT security review (issue #16)

Scope: spec section 12 (14 mandatory protections), section 18 rule 9, and the extra checklist items of issue #16.
PACT is a **sandbox reference implementation**, not a certified financial security product. Test ids below are
`file::test`; `S::` abbreviates `tests/test_security.py::`. The suite is run with `uv run pytest`.

## Threat model

| Asset | Threat | Primary controls |
|---|---|---|
| PayPal client secret / access token | Leaks to the LLM, browser, logs or audit | Held only in the server-side adapter (`_Secret`, redacted in repr and error text); `SecretStr` in settings; MCP process has no provider or admin credentials |
| Admin token | Agent or web user approves/edits policy | `/admin/*` needs `PACT_ADMIN_TOKEN` (constant-time compare); agent keys get 403; storefront source never references it; token lives in `sessionStorage` of the admin SPA only |
| Agent API keys | Theft, impersonation, cross-agent access | Only SHA-256 stored; requests are scoped to the authenticated agent (other agents' requests are "not found"); keys file mode 600; rotation |
| Authorization and grants | Self-approval, replay, tampering, order swap | Deterministic policy engine, hash-bound approval, single-use grants bound to merchant/order/amount/currency/request hash, live-order re-check at execute, atomic consume |
| Money | Double spend, over-budget, double capture | Atomic budget reservations, deterministic provider idempotency keys, UNKNOWN state + reconciliation, never auto-release on ambiguity |
| Audit trail | Silent edit/deletion | Hash chain + head row, `verify_chain`, `/admin/audit/verify` |
| Prompt injection | Model told (by web content or chat) to approve, change policy, read secrets | No approve/deny/policy/secret tool exists; the MCP client only holds an agent key; instructions are advisory, enforcement is server-side |

Trust boundaries: the **LLM/agent** is untrusted (can only call `/v1/*`); the **browser** is untrusted
(storefront/Merchant Lab routes, provider redirects); the **operator** holding the admin token is trusted;
the **payment provider** is trusted for facts only after PACT verifies them (capture amount/currency are re-checked).

## Checklist to test matrix

| # | Protection | Existing tests | Added in `test_security.py` |
|---|---|---|---|
| 1 | Server-side credentials (PayPal secret, admin token, agent keys, grant tokens never in responses, logs, audit, MCP output) | `test_paypal_adapter::test_repr_and_vars_hide_secrets`, `::test_raw_never_contains_token_or_secret`, `::test_error_messages_never_contain_secrets_even_if_echoed`, `::test_oauth_failure_messages_never_contain_secrets`, `::test_timeout_messages_never_contain_secrets`; `test_config::test_secrets_not_in_repr_or_str`, `::test_secrets_not_in_validation_errors`, `::test_credentials_embedded_in_base_url_not_echoed`; `test_grants::test_token_stored_only_as_hash`, `::test_token_not_in_claim_repr`; `test_storefront::test_status_response_never_contains_secret_values`; `test_keys_file::*` | `S::test_secrets_never_appear_in_responses_logs_audit_or_database` (full flow incl. failures; scans ~30 responses, `caplog` at DEBUG, audit JSON, raw SQLite+WAL bytes), `S::test_mcp_responses_never_contain_secrets`, `S::test_mcp_client_holds_only_the_agent_key_and_gets_403_on_admin` |
| 2 | Agent identity: invalid/disabled key 401/403, cross-agent denied | `test_api::test_agent_routes_require_valid_key`; `test_authorization::test_authentication`, `::test_requests_are_private_to_their_agent`; `test_gateway::test_wrong_token_and_wrong_agent`; `test_grants::test_different_agent_rejected`; `test_convenience::test_rotate_agent_key` | `S::test_missing_malformed_and_unknown_credentials_are_rejected`, `S::test_disabled_agent_is_rejected_on_every_agent_route`, `S::test_rotated_key_stops_working_immediately`, `S::test_cross_agent_access_is_denied_over_http`, `S::test_agent_cannot_request_authorization_for_someone_elses_identity` |
| 3 | User + merchant policy enforcement | `test_policy_engine::*` (76), `test_authorization::test_below_threshold_*`, `::test_above_threshold_*`, `::test_unapproved_merchant_*`, `::test_agent_transaction_limit` | (covered) |
| 4 | Transaction binding (amount, merchant, order, currency, hash) | `test_grants::test_binding_mismatch`, `::test_tampered_request_row_hash_rejected`, `::test_tampered_grant_hash_rejected`; `test_gateway::test_modified_transaction_rejected_and_grant_still_usable`; `test_mcp::test_tampered_amount_is_rejected_via_mcp`; `test_authorization::test_approval_requires_displayed_hash` | `S::test_approval_needs_the_displayed_hash_over_http` |
| 5 | Single-use grants | `test_grants::test_replay_rejected`, `::test_concurrent_consume_exactly_one_wins`; `test_gateway::test_replayed_grant_is_rejected_and_audited`, `::test_concurrent_execution_with_one_grant`; `test_api::test_full_purchase_over_http_then_replay_rejected`; `test_authorization::test_approval_then_single_delivery_of_grant` | (covered) |
| 6 | Atomic budget reservations (thread race) | `test_budget::test_twenty_threads_race_for_budget_fitting_exactly_five`, `::test_cross_agent_contention_under_one_org_budget`; `test_authorization::test_concurrent_requests_never_overspend`, `::test_shared_budget_across_agents` | (covered) |
| 7 | Idempotent execution (PayPal-Request-Id + execute key) | `test_paypal_adapter::test_create_order_request_shape`, `::test_capture_request_shape`, `::test_check_script_capture_key_is_deterministic`; `test_simulation_adapter::test_create_is_idempotent_per_key`, `::test_capture_is_idempotent_with_same_key`; `test_gateway::test_idempotent_retry_returns_same_execution`, `::test_double_return_settles_once` | (covered) |
| 8 | Replay prevention | `test_grants::test_replay_rejected`; `test_gateway::test_replayed_grant_is_rejected_and_audited`; `test_authorization::test_duplicate_authorization_for_same_order_denied` | (covered) |
| 9 | Expiry (approval + grant) | `test_authorization::test_pending_approval_expires`, `::test_approval_after_window_is_refused`, `::test_unused_grant_expires`; `test_grants::test_expired_grant_rejected`; `test_gateway::test_expired_grant_rejected_and_released` | (covered) |
| 10 | Revocation | `test_grants::test_revoke_*`; `test_authorization::test_revoke_unused_authorization`; `test_gateway::test_revoked_grant_rejected`; `test_api::test_deny_and_revoke_routes` | (covered) |
| 11 | Tamper-evident audit (`verify_chain`) | `test_audit::test_editing_any_field_breaks_chain_at_that_seq`, `::test_deleting_*`, `::test_appending_forged_event_without_head_is_detected`, `::test_tampering_head_seq_is_detected` (documented gap: `::test_truncation_with_head_rollback_needs_external_anchor`) | (covered) |
| 12 | Fail-closed evaluation | `test_policy_engine::test_injected_exception_fails_closed`, `::test_malformed_policy_document_fails_closed`, `::test_missing_required_policy_fails_closed`, `::test_garbage_arguments_fail_closed`; `test_authorization::test_missing_policy_fails_closed` | `S::test_corrupt_policy_document_denies_over_http`, `S::test_engine_exception_denies_and_never_grants` |
| 13 | Modification after approval detected | `test_grants::test_order_modified_after_approval`; `test_gateway::test_order_changed_after_grant_is_rejected`; `test_authorization::test_order_modified_before_approval_is_denied` | `S::test_order_modified_after_approval_is_rejected_at_execute` (PATCH over HTTP, both original and new amounts, audited) |
| 14 | Reconciliation after timeouts | `test_gateway::test_timeout_on_create_goes_unknown_then_reconciles_without_duplicate`, `::test_timeout_on_capture_after_side_effect_reconciles_once`, `::test_status_poll_captures_after_buyer_approval`; `test_paypal_adapter::test_5xx_and_408_map_to_timeout`, `::test_unreadable_success_body_is_outcome_unknown` | (covered) |
| A | Admin endpoints reject agent keys | `test_api::test_agent_key_cannot_use_admin_routes`, `::test_admin_token_never_accepted_as_agent`, `::test_admin_disabled_without_token`; `test_storefront::test_storefront_cannot_approve`; `test_convenience::test_cancel_requires_admin_and_live_state` | `S::test_admin_routes_reject_agent_keys_and_bad_tokens` (every admin route x 7 bad credentials + query/header smuggling), `S::test_every_registered_admin_route_is_in_the_matrix` (fails if a new `/admin` route is added untested), `S::test_agent_cannot_approve_its_own_request_so_nothing_is_granted` |
| B | Timing-safe comparison | `test_grants::test_token_compared_in_constant_time` | `S::test_admin_comparison_is_constant_time` (spy on the call the server makes), `S::test_agent_key_lookup_uses_constant_time_compare`, `S::test_no_server_module_compares_secrets_with_plain_equality` |
| C | Prompt injection: no MCP tool can approve, edit policy, read secrets | `test_mcp::test_tool_surface_has_no_approval_capability`; `test_convenience::test_instructions_forbid_self_management` | `S::test_mcp_tools_cannot_approve_edit_policy_or_read_secrets` (introspects `build_server`: names and input schemas), `S::test_mcp_evaluate_policy_is_read_only`, `S::test_policy_update_requires_admin_and_rejects_invalid_rules`, `S::test_there_is_no_agent_route_that_writes_policy_or_approves`, `S::test_storefront_source_never_touches_the_admin_api_or_token`, `S::test_storefront_cannot_start_anything_but_pact_governed_payments` |
| D | `/security-review` of the branch | This document; section "Findings" below | `S::test_no_cors_headers_are_emitted`, `S::test_error_bodies_do_not_leak_internals`, `S::test_oversized_and_unknown_fields_are_rejected`, `S::test_reflected_values_are_html_escaped`, `S::test_static_dashboard_mount_does_not_serve_files_outside_dist` + six strict xfails for open findings |

Every checklist item is covered by at least one test. Items 3, 5-11 and 14 had thorough unit-level coverage and
were not duplicated. Item 1's "MCP output" is covered through the REST responses the MCP tools forward
(`S::test_mcp_responses_never_contain_secrets`); the MCP process itself holds no secrets other than the agent key.

## Findings

Severity is for a sandbox deployment on a trusted network; for any real exposure treat Medium as High.
"Test" names the strict xfail that flips when the fix lands (remove the marker then). Source files were not
modified by this review.

### SEC-1 (Medium) Anyone can cancel a live payment: unauthenticated state-changing GET
- Where: `pact_server/app.py:355-361` (`/payments/cancel`), `pact_core/gateway.py:213-233` (`handle_provider_cancel`);
  the id is leaked by `pact_server/storefront.py:47` (`provider_order_id` in the public `/merchant/orders/{id}/status`)
  and discoverable via the public `/merchant/transactions`.
- Exploit: attacker lists orders, reads `provider_order_id` of any EXECUTING payment, calls
  `GET /payments/cancel?token=<id>`. The adapter reports the order not COMPLETED, so PACT marks the request
  FAILED (`BUYER_CANCELLED`) and releases its reservation. The legitimate buyer (human or agent) loses the checkout;
  repeated, it is a targeted denial of service on any purchase. With PayPal the order is later approved by the buyer
  but never captured (state already terminal), which is confusing but not a money loss. A prefetching browser,
  link scanner or `<img src>` can trigger it too.
- Fix: bind the redirect to the buyer: put an unguessable per-request nonce (HMAC of request id with a server
  secret) into `return_url`/`cancel_url` and require it; do not treat the bare provider order id as proof. Make
  state changes POST, or make cancel only record intent and let reconciliation decide. Stop returning
  `provider_order_id` from the public status route.
- Status (after #40, `fix/paypal-gateway`): **partly fixed.** `GET /payments/cancel` no longer ends a payment; it only
  records `BUYER_CANCELLED` on a request that stays `EXECUTING`, so prefetchers, link scanners and `<img src>` are
  harmless. Still open: `POST /payments/abandon` (the buyer's explicit cancel) is unauthenticated and accepts the same
  leaked provider order id, so a deliberate attacker can still end a live, uncaptured payment. It cannot touch a
  payment whose capture is in flight, in doubt or done. The nonce fix above is still needed, and the public status
  route still returns `provider_order_id`.
- Tests: `test_anonymous_party_cannot_cancel_someone_elses_live_payment` (GET, passes),
  `test_anonymous_party_cannot_abandon_someone_elses_live_payment` (POST, expected failure until fixed).

### SEC-2 (Low/Medium) No framing or content-type protection
- Where: `pact_server/app.py` (no middleware; every route incl. the StaticFiles mount at line 402, `_page()` at 420).
- Exploit: an attacker page frames the dashboard (admin Approve button) or the simulated checkout and tricks the
  operator into clicking (clickjacking). Admin actions still need the token in the operator's session storage.
- Fix: middleware adding `X-Frame-Options: DENY` (or CSP `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, and a CSP for the HTML pages.
- Test: `test_responses_forbid_framing_and_sniffing` (SEC-2).

### SEC-3 (Low) Path injection in MCP tool arguments
- Where: `pact_mcp/server.py:102` (`merchant_get_product`), `:169`, `:180`, `:202`, `:209`, `merchant_get_order`
  (`f"/merchant/orders/{order_id}"`): ids are interpolated into URL paths without quoting.
- Exploit: a prompt-injected model passes `product_id="../../v1/me"`; httpx normalises it and the agent's
  authenticated client calls an arbitrary API path. Impact is bounded because the client only holds an agent key
  (admin routes give 403, suffixes like `/execute` are fixed), but it widens what injection can reach (any GET
  under `/v1` or `/merchant`).
- Fix: `urllib.parse.quote(value, safe="")` every path segment (or reject ids not matching `^[A-Za-z0-9_:.-]+$`).
- Test: `test_mcp_ids_cannot_traverse_to_other_api_paths` (SEC-3).

### SEC-4 (Medium) Weak or placeholder admin token accepted
- Where: `pact_core/config.py:34` (`admin_token: SecretStr | None`, no validation), `.env.example:16`
  (`change-me-to-a-long-random-string`), `pact_server/app.py:125`.
- Exploit: an operator who copies `.env.example` (or picks a short token) leaves a guessable credential guarding
  approvals and policy; combined with SEC-5 it is brute-forceable. The admin token is the single root of trust.
- Fix: reject tokens shorter than 32 characters and the known placeholder at startup (`pact doctor` should flag it);
  have `pact init` generate one.
- Test: `test_weak_or_placeholder_admin_tokens_are_rejected_at_startup` (SEC-4). Note `test_config::test_env_example_template_loads`
  loads the example file and will need updating together with the fix.

### SEC-5 (Low/Medium) No rate limiting on authentication
- Where: `pact_server/app.py:170-179` (`agent_auth`, `admin_auth`), `pact_core/core.py:42`.
- Exploit: unlimited online guessing of the admin token or agent keys (each attempt also costs a DB transaction
  for agent keys). Agent keys are 256-bit and infeasible to guess; the admin token is operator-chosen (SEC-4).
- Fix: per-IP/per-route throttling with a short lockout on repeated 401/403 (reverse proxy or middleware).
- Test: `test_repeated_bad_admin_tokens_are_throttled` (SEC-5).

### SEC-6 (Low) Unbounded memory from unauthenticated checkout of unknown ids
- Where: `pact_server/storefront.py:86` (`self._locks[order_id]` is a defaultdict created before `get_order` runs at line 91).
- Exploit: `POST /merchant/orders/<random>/checkout` in a loop adds one `threading.Lock` per id, forever.
- Fix: look the order up first (404 before touching the dict), or use a bounded/striped lock pool.
- Test: `test_unknown_order_ids_do_not_allocate_server_state` (SEC-6).

### Reviewed, no vulnerability found
- AuthZ: every `/v1` route depends on `agent_auth`, every `/admin` route on `admin_auth` (enforced by
  `test_every_registered_admin_route_is_in_the_matrix`); request ownership is checked in `PactContext.load_request`,
  which reports other agents' requests as not found. `agent_identity("ag_storefront")` is never reachable with a
  caller-supplied id.
- Secrets in logs: the only log statements are the sweeper's (`pact_server/app.py:138,140`); none print inputs. The
  PayPal adapter redacts secret and token from error text. Verified by the caplog assertion at DEBUG level.
- SSRF: the PayPal base URL is validated as sandbox/local at config time; provider order ids reach the network
  only after being looked up from the DB or matching the adapter's strict id pattern; PACT never fetches
  agent-supplied URLs. `return_url`/`cancel_url` are built from `PACT_PUBLIC_URL`, so `sim_approve`'s redirect is not an open redirect.
- Injection: SQLAlchemy parameter binding throughout; LIKE wildcards escaped in `search_products`; HTML pages escape
  every interpolated value (`test_reflected_values_are_html_escaped`); static mount rejects traversal.
- CORS: no CORS middleware, so browsers enforce same-origin on the API (`test_no_cors_headers_are_emitted`).
- Header handling: only `Authorization: Bearer` is read; credentials in query strings or other headers are ignored.
- Error bodies: validation errors return only `loc` and `msg`, never the submitted value.
- `/payments/return` triggers capture only after the provider reports buyer approval (`NOT_READY` otherwise) and
  re-verifies amount and currency, so a forged hit cannot move unauthorised money.

## Honest limitations

- **Sandbox reference implementation.** Not audited, not PCI/PSD2 scope; only the PayPal Sandbox or the local
  simulation is permitted (`PAYPAL_BASE_URL` is validated to refuse live endpoints). No real money.
- **Merchant Lab and the storefront routes are unauthenticated by design** (`/merchant/*`, including order
  creation, `PATCH` of unpaid orders, transactions listing, web checkout and order status). This is a demo
  simplification so a browser shopper and an agent can share one fake merchant. Consequences: anyone who can reach
  the port can create/modify unpaid orders (an approved agent order can be made to fail with `ORDER_MODIFIED`,
  which is detected but is a denial of service), enumerate orders, and in simulation mode approve or cancel any
  simulated checkout (`/sim/*`).
- **Web checkout can only start PACT-governed requests.** It acts as the server-side `ag_storefront` identity under
  its own policy and the shared budgets; it cannot approve, never receives a grant token, and cannot touch an
  order an agent owns or has in flight. It does mean an anonymous visitor can spend `ag_storefront` budget
  (subject to approval) on any unpaid order that is not agent-owned.
- Single admin credential, no per-operator identity, no MFA; the audit log records `admin:dashboard`.
- The audit chain proves tampering only against an external anchor of the head hash
  (`test_truncation_with_head_rollback_needs_external_anchor`).
- Agent keys are stored as unsalted SHA-256 (adequate for 256-bit random keys, not for human passwords).
- No TLS termination, CSRF concerns do not apply (bearer header auth), SQLite is a single-node store.
- Prompt injection is mitigated structurally (no approval path exists for an agent), not by detection: a
  manipulated agent can still *request* payments within its policy and budget; the human approval and budget
  caps are the backstop.
