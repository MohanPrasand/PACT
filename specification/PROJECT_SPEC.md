# PACT
## Project Definition, Architecture & Execution Specification

**Project:** PACT — Policy and Authorization Control Tower
**Category:** AI Agent Infrastructure / FinTech / Agentic Commerce
**Hackathon:** PayPal AI Hackathon — Build What's Next with PayPal and AI
**Version:** 0.1 — Hackathon MVP
**Implementation:** Solo developer
**Primary interface:** Model Context Protocol (MCP)
**Primary payment integration:** PayPal Sandbox
**Primary demonstration client:** Claude Code

> Implementation notes (deviations decided during build, tracked in GitHub issues):
> - Demo currency defaults to **USD** (`PACT_CURRENCY`). PayPal supports INR only for in-country India accounts (§9.2 asks to verify this).
> - MCP tool names use `_` instead of `.` (e.g. `pact_request_authorization`) because Claude tool names must match `^[a-zA-Z0-9_-]{1,64}$`.
> - The MCP server uses `MCPServer` from the official `mcp` SDK 2.x (the successor of FastMCP) and is a thin HTTP client over the PACT REST API using an agent key.
> - Malformed policy documents fail closed with `<SCOPE>_POLICY_INVALID`; the merchant policy document is optional (no merchant restrictions when absent).
> - A Merchant Lab storefront UI (catalogue, live cart, order-status timeline) was added for demo visibility: issues #22–#25. It never initiates payments, and it adds three MCP cart tools (`merchant_add_to_cart`, `merchant_view_cart`, `merchant_checkout_cart`).

---

# 1. Executive Summary

PACT is a financial governance runtime for autonomous AI agents.

It provides a centralized mechanism for controlling, authorizing, monitoring and auditing financial actions initiated by AI agents.

As AI agents become capable of purchasing products, paying invoices, managing subscriptions, issuing refunds and conducting business operations, unrestricted access to payment APIs becomes a significant security and financial risk.

Existing payment systems provide transaction processing. Agent protocols provide mechanisms for exposing tools and communicating authorization intent. However, developers still need to implement financial policies, spending coordination, transaction lifecycle management and enforcement across their individual agent applications.

PACT aims to provide this missing operational layer. It allows developers and merchants to define financial policies once and enforce them consistently across multiple agents and payment integrations.

**Core proposition:** Define financial authority once. Enforce it consistently across agents, tools and payment providers.

PACT is not a payment gateway, wallet, payment processor or replacement for existing authorization protocols. It is an execution-time financial governance layer.

# 2. Problem Statement

Consider an organization operating three AI agents:

- Shopping agent: purchases products.
- Research agent: purchases information and datasets.
- Procurement agent: pays approved suppliers.

Each agent has access to a payment integration. The organization defines a daily financial budget of ₹10,000.

Without centralized governance, the agents might independently spend:

| Agent | Spending |
|---|---:|
| Shopping | ₹4,000 |
| Research | ₹3,000 |
| Procurement | ₹5,000 |
| Total | ₹12,000 |

The organization has exceeded its budget even though each agent might individually satisfy its own spending restrictions.

Other problems include:

1. Agents modifying transaction details after approval.
2. Multiple agents attempting duplicate payments.
3. Individual spending limits not reflecting aggregate expenditure.
4. Merchant policies conflicting with user policies.
5. Unauthorized tool calls.
6. Replayed or expired authorization grants.
7. Insufficient auditability.
8. Difficulty maintaining consistent financial controls across different providers.

PACT addresses these problems through centralized policy evaluation and execution enforcement.

# 3. Product Definition

| Component | Responsibility |
|---|---|
| PACT Core | Authorization lifecycle and policy engine |
| PACT MCP | Agent-facing MCP interface |
| PACT Budget Engine | Shared budgets and reservations |
| PACT Execution Gateway | Validates and executes authorized actions |
| PACT Merchant Lab | Simulated merchant and commerce environment |
| PACT Dashboard | Policy management, approvals and audit history |

The PayPal adapter connects the execution gateway to the PayPal Orders API.

## 3.1 Intended users

- **Developers** integrate PACT into AI agents without independently implementing the complete financial governance lifecycle.
- **Merchants** configure acceptable agent behavior, transaction limits and approval requirements.
- **Organizations** enforce centralized financial budgets across multiple agents and business processes.
- **End users** maintain control over the financial authority delegated to their AI agents.

# 4. Product Boundaries and Non-Goals

This section is mandatory. All implementation decisions must respect it.

## 4.1 What PACT IS

- A financial governance runtime.
- A policy evaluation engine.
- A shared budget management system.
- An authorization lifecycle manager.
- An execution-time validation layer.
- An MCP server for AI agents.
- A reference integration with PayPal Sandbox.

## 4.2 What PACT IS NOT

PACT must not become: a replacement for PayPal, Stripe, Visa or Mastercard; a new payment processing network; a cryptocurrency wallet; a custodial financial service; a replacement for Google AP2; a universal payment credential standard; a general-purpose MCP gateway; a generic AI chatbot; a complete ecommerce platform; a fraud detection research platform; a multi-agent orchestration framework.

## 4.3 Strict scope rule

Every feature must directly support at least one of these objectives:

1. Financial policy evaluation.
2. Shared financial budget enforcement.
3. Authorization and approval.
4. Secure transaction execution.
5. Financial auditability.
6. PayPal payment integration.

If a feature does not contribute to these objectives, it must not be implemented in the hackathon MVP.

# 5. Core Architectural Philosophy

1. **Agents request; they do not authorize themselves.** An AI agent can propose a financial action but cannot grant itself permission.
2. **Policies are deterministic.** The LLM must not decide whether a payment is allowed. The policy engine makes that decision using deterministic rules.
3. **Authorization must be transaction-bound.** Approval of one transaction cannot be reused for another transaction with a different merchant, amount, currency or order.
4. **Budgets are globally coordinated.** Independent agents share a common budget ledger. Concurrent transactions must not cause budget overspending.
5. **Payment execution must be auditable.** Every financial decision must have a corresponding record containing the request, policy evaluation, authorization evidence and execution result.

# 6. System Architecture

## 6.1 High-level architecture

```
AI AGENTS (Claude Code, custom Python agents, other MCP clients)
        ↓
PACT MCP SERVER / REST API
        ↓
PACT GOVERNANCE ENGINE
  identity & request validation · policy evaluation · budget reservations
  risk checks · approval management · grant management
        ↓
PACT EXECUTION GATEWAY
  grant verification · transaction binding · idempotency · execution state
        ↓
PAYMENT ADAPTERS (PayPal Sandbox · Simulation · future providers)
        ↓
PERSISTENCE (policies · requests · budgets · reservations · grants · transactions · audit events)
```

The merchant simulator operates as an independent commerce service connected to the execution gateway. The frontend dashboard communicates with PACT through authenticated REST endpoints.

## 6.2 Recommended technology stack

| Layer | Technology |
|---|---|
| Backend | Python |
| API | FastAPI |
| MCP | FastMCP (official SDK `MCPServer`) |
| Validation | Pydantic |
| Database | SQLite initially |
| ORM | SQLAlchemy |
| Frontend | React |
| Styling | Tailwind CSS |
| AI integration | Claude Code MCP client |
| Payment integration | PayPal REST Orders API v2 |
| Testing | Pytest |
| Deployment | Render or another suitable host |

PostgreSQL can replace SQLite when deployment requires concurrent access and persistent shared state. Do not introduce Redis, Kafka, Kubernetes or distributed microservices into the initial implementation.

# 7. Core Components

## 7.1 PACT Core

Primary business logic layer. Responsibilities: validate authorization requests; maintain transaction state; evaluate policies; coordinate budget reservations; manage approvals; issue and revoke grants; coordinate execution. It must remain independent of MCP and payment providers, so the same logic serves REST clients, MCP clients and future SDKs.

## 7.2 PACT Policy Engine

Evaluates a financial action against multiple restrictions. Policy sources: user, merchant, organization, agent-specific, system-level security policy.

Decisions: `ALLOW`, `DENY`, `REQUIRE_APPROVAL`. A mandatory deny overrides any allow decision. Human approval cannot override hard system restrictions, invalid authorization or insufficient funds/budget reservations.

Example — User: daily limit ₹10,000, approval threshold ₹2,000. Merchant: max transaction ₹5,000, approved merchants only. Agent: max transaction ₹3,000. System: no duplicate payment execution, no expired grants. A payment must satisfy all mandatory restrictions.

The engine returns a structured decision containing: decision, reason codes, policies evaluated, applicable limits, required approval, policy version. The LLM may explain the result in natural language but must never modify it.

## 7.3 PACT Budget Engine

Maintains shared budgets across agents. Example: global budget ₹10,000; agent limits Shopping ₹5,000, Research ₹2,000, Procurement ₹5,000. Individual limits do not create separate independent budgets; the global limit remains authoritative.

Tracks: available balance, reserved amount, settled spending, released reservations, refunded amounts, pending transactions.

**Budget invariant:** Available budget + Reserved budget + Net settled expenditure = Global budget.

Refunds and reversals must be recorded as explicit ledger events. Budget reservations must be atomic — two concurrent requests must not both reserve the same remaining funds. A pending authorization should reserve its maximum authorized amount where appropriate. Failed or cancelled transactions release reservations only after their execution state is reconciled.

## 7.4 Authorization Manager

States: `CREATED`, `EVALUATING`, `DENIED`, `PENDING_APPROVAL`, `APPROVED`, `GRANTED`, `EXECUTING`, `EXECUTED`, `FAILED`, `EXPIRED`, `REVOKED`, `UNKNOWN`. Transitions must be validated. A denied request cannot transition directly into execution. An expired grant cannot be reused. An executed grant cannot be consumed again.

## 7.5 Grant Manager

A grant is a restricted internal permission to execute an approved action. It is not a payment credential, a PayPal access token, a wallet, or a transferable payment token.

Each grant binds: request ID, agent identity, user identity, merchant identity, order identity, action type, amount, currency, request hash, expiration time, single-use status. Use cryptographically random opaque identifiers; store a secure hash of the grant secret; validate and consume atomically.

## 7.6 PACT Execution Gateway

The only component allowed to initiate financial execution through payment adapters. Before execution it must:

1. Authenticate the caller.
2. Load the authorization request.
3. Verify grant validity.
4. Verify request hash.
5. Verify merchant and order.
6. Verify amount and currency.
7. Verify budget reservation.
8. Verify that the grant is unused.
9. Apply an idempotency key.
10. Transition the request into execution.

If the provider times out, the transaction must enter an `UNKNOWN` state. Do not blindly retry a payment that might already have been captured. Reconcile the provider state before attempting another execution.

# 8. Merchant Lab

A small simulated commerce environment where agents discover products, create orders and attempt purchases. Not intended to compete with ecommerce platforms.

- **Product catalogue:** mechanical keyboard, wireless mouse, monitor, headphones, USB hub. Fields: product ID, name, description, price, currency, stock, merchant ID.
- **Shopping cart:** add, remove, view, totals.
- **Order service:** create, retrieve, update state, link to authorization requests.
- **Merchant policy configuration:** maximum purchase amount, allowed product categories, agent restrictions, approval requirements.
- **Payment status:** Created, Pending, Approved, Captured, Failed, Cancelled.

**Separation:** Merchant Lab creates commercial orders. PACT authorizes their financial execution. PayPal processes the sandbox payment.

# 9. PayPal Integration

PayPal must be central. Use PayPal Sandbox and the Orders API v2 (https://developer.paypal.com/api/rest/integration/orders-api).

## 9.1 Payment workflow

1. Merchant Lab creates an order.
2. PACT evaluates the order.
3. PACT obtains required user approval.
4. PACT issues an internal execution grant.
5. The PayPal adapter creates the PayPal order.
6. The customer completes PayPal buyer approval.
7. The backend captures the approved PayPal order.
8. The provider result is verified.
9. PACT records the transaction.

The buyer approval step is separate from PACT's application-level authorization.

## 9.2 Required implementation

Sandbox credentials; OAuth access token generation; order creation; approval URL handling; buyer approval; payment capture; order status retrieval; error handling; idempotency through PayPal request identifiers; transaction reconciliation.

Never expose PayPal client secrets or access tokens to the browser or LLM. Never present simulated payments as PayPal transactions. Verify currency eligibility before building the demo around INR.

# 10. PACT MCP Server

## 10.1 Merchant tools

| Tool | Purpose |
|---|---|
| merchant.search_products | Search the product catalogue |
| merchant.get_product | Retrieve product information |
| merchant.create_order | Create a merchant order |
| merchant.get_order | Retrieve order details |
| merchant.list_transactions | Retrieve transaction history |

## 10.2 PACT tools

| Tool | Purpose |
|---|---|
| pact.request_authorization | Submit a financial action |
| pact.evaluate_policy | Evaluate policy restrictions |
| pact.get_authorization_status | Retrieve request status |
| pact.execute_action | Execute using a valid grant |
| pact.revoke_grant | Revoke an unused grant |
| pact.get_audit_log | Retrieve financial decision history |
| pact.get_budget | Retrieve shared budget status |

Approval and denial are performed through a trusted dashboard or authenticated approval interface. An agent must not be able to approve its own financial action by invoking another MCP tool.

## 10.3 MCP architecture rule

MCP is an interface, not the authorization engine. Authorization logic lives in PACT Core.

# 11. End-to-End Workflow

User instruction in Claude Code: *"Find the best mechanical keyboard under ₹5,000 and purchase it."*

1. **Product discovery** — `merchant.search_products`; Claude compares and selects.
2. **Order creation** — `merchant.create_order` (₹4,200).
3. **Authorization request** — `pact.request_authorization` with agent, user, merchant, order ID, amount, currency, action type.
4. **Policy evaluation** — user budget, merchant limits, agent limits, org restrictions, duplicates → requires human approval.
5. **Approval** — dashboard shows merchant, product, order ID, amount, currency, applicable policies, consequences; user approves the exact transaction.
6. **Grant creation** — short-lived, transaction-bound grant.
7. **Execution** — gateway validates and atomically consumes the grant; PayPal order created; buyer approves; backend captures.
8. **Verification** — PACT verifies the PayPal result; order marked paid only after confirmation.
9. **Audit** — request, agent, policy evaluation, approval, grant, execution, PayPal reference, final result.

# 12. Security Requirements

1. Server-side payment credential management.
2. Agent identity validation.
3. User and merchant policy enforcement.
4. Transaction-bound authorization.
5. Single-use grants.
6. Atomic budget reservations.
7. Idempotent payment execution.
8. Replay prevention.
9. Expiry enforcement.
10. Revocation.
11. Immutable or tamper-evident audit events.
12. Fail-closed behavior for policy evaluation failures.
13. Protection against transaction modification after approval.
14. Reconciliation after provider timeouts.

Do not allow the LLM to directly access payment secrets. Do not trust natural-language instructions as authorization evidence. Do not allow prompt injection to override system policies. This is a sandbox reference implementation, not a certified financial security product.

# 13. Dashboard Requirements

- **Overview:** global budget, available, reserved, settled, active agents, recent transactions.
- **Policy Management:** global limit, agent limits, merchant restrictions, approval thresholds, duplicate restrictions.
- **Approval Centre:** pending requests, agent, merchant, amount, applicable policies, approve/deny.
- **Audit Explorer:** authorization history, policy decisions, grant lifecycle, execution outcomes, provider references, rejected replay attempts.

The dashboard must be functional, not a static design.

# 14. Testing Strategy

**14.1 Policy:** below threshold → Allow; above threshold → Require approval; above transaction limit → Deny; unknown merchant → Deny; agent exceeds own limit → Deny; global budget exhausted → Deny.

**14.2 Budget:** concurrent reservations; partial reservation failure; successful settlement; failed payment release; cancellation; refund accounting; cross-agent contention.

**14.3 Grants:** valid; expired; revoked; replayed; modified amount; modified merchant; modified order; incorrect request hash.

**14.4 Execution:** successful PayPal capture; buyer cancellation; provider failure; provider timeout; duplicate execution; reconciliation.

**14.5 End-to-end acceptance:** Claude purchases a product → PACT requires approval → user approves → PayPal Sandbox completes payment → PACT records result → agent reuses the grant → PACT rejects it.

# 15. Implementation Plan

1. **Core foundation** — setup, Pydantic models, DB models, state machine, policy engine, unit tests.
2. **Budget and grants** — ledger, atomic reservations, agent limits, grant generation/consumption, expiry/revocation, idempotency.
3. **Merchant Lab** — catalogue, cart, orders, merchant policies, order state.
4. **PayPal integration** — sandbox credentials, OAuth, Orders API, buyer approval, capture, status verification.
5. **MCP integration** — server, merchant tools, PACT tools, Claude Code config, E2E MCP testing.
6. **Dashboard** — overview, policy management, approval centre, audit explorer.
7. **Stabilization** — security testing, concurrency testing, provider error handling, deployment, docs, video.

Prioritize a complete working transaction over visual polish or additional features.

# 16. Expected Deliverables

PACT Core; MCP server; Merchant Lab; PayPal Sandbox adapter; dashboard; database models; protocol specification; policy examples; test suite; Claude Code MCP configuration example; setup instructions; environment variable documentation; architecture diagram; MIT licence; demo video.

# 17. Competitive Positioning

PACT operates alongside Google AP2, Visa Intelligent Commerce, Mastercard Agent Pay, Stripe agentic payments, PayPal Agent Ready and MCP governance tools. It must not claim to invent agent payment authorization, delegated permissions, spending limits, human approval, secure credentials or MCP tool governance.

Differentiation is the practical combination of: shared budgets across agents; composition of user, merchant and organizational policies; transaction lifecycle management; execution-time enforcement; provider-independent governance interfaces; auditable decision traces. PACT complements existing protocols; it does not replace them.

# 18. Strict Scope Guardrails

1. No feature without a direct purpose.
2. No custom LLM policy decisions.
3. No multi-agent framework.
4. No real-money payment processing.
5. No unnecessary infrastructure.
6. No premature provider expansion (PayPal + simulation first).
7. No generic ecommerce platform.
8. No replacement protocols.
9. Security before convenience.
10. A working product beats an ambitious architecture.

# 19. What Success Looks Like

1. Claude Code connects through MCP.
2. Multiple agents share a financial budget.
3. Policies are evaluated deterministically.
4. Conflicting policies are handled predictably.
5. A purchase can require human approval.
6. The approval is bound to the exact order.
7. A restricted grant is issued.
8. PayPal Sandbox completes a real sandbox transaction.
9. Replay and modification attempts are rejected.
10. Every decision is visible in the audit explorer.
11. The project installs and tests from its public repository.

# 20. Hackathon Presentation (< 3 min)

0:00 Problem · 0:25 Architecture (Claude Code ↔ MCP ↔ PACT) · 0:50 Shared budget · 1:20 Authorization & policy decision · 1:55 PayPal Sandbox checkout & capture · 2:25 Grant replay rejected · 2:45 Impact.

# 21. Official References

- https://paypalaihackathon.devpost.com/rules
- https://developer.paypal.com/community/blog/PayPal_AI_Hackathon/
- https://developer.paypal.com/api/rest/integration/orders-api
- https://developer.paypal.com/ai-tools/mcp-server
- https://developer.paypal.com/agent-ready/overview/
- https://modelcontextprotocol.io/

---

**Final directive:** AI Agent → MCP → PACT Policy Engine → Shared Budget → Human Approval → Restricted Grant → PayPal Sandbox → Audit. Build the smallest complete system that convincingly demonstrates the value of PACT.
