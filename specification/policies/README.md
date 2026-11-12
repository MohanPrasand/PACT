# Policy reference

Policies are JSON documents, one per scope and subject, validated strictly: unknown fields, negative limits and type coercion (e.g. `"5000"`) are rejected. All amounts are integer **minor units** (cents for USD).

The files in this directory are the demo configuration and are validated by the test suite.

| File | Policy id (used by seed data) | Scope |
|---|---|---|
| `system.json` | `system` | system |
| `org.json` | `org:default` | org |
| `user.json` | `user:u_demo` | user |
| `merchant.m_techmart.json` | `merchant:m_techmart` | merchant |
| `merchant.m_datahub.json` | `merchant:m_datahub` | merchant |
| `agent.ag_shopping.json` | `agent:ag_shopping` | agent |
| `agent.ag_research.json` | `agent:ag_research` | agent |
| `agent.ag_procurement.json` | `agent:ag_procurement` | agent |
| `agent.ag_storefront.json` | `agent:ag_storefront` | agent (web checkout identity) |

## Schemas

### system (hard restrictions, never overridden by approval)
| Field | Type | Default |
|---|---|---|
| `currency` | `^[A-Z]{3}$` | `USD` |
| `hard_max_transaction_minor` | int ≥ 1 | `100000` |
| `allowed_actions` | list[str] | `["PURCHASE"]` |

### org
| Field | Type | Default |
|---|---|---|
| `daily_budget_minor` | int ≥ 0 | required (shared by all agents) |
| `approved_merchants` | list[str] | `[]` |
| `max_transaction_minor` | int ≥ 1 or null | null |
| `duplicate_window_minutes` | int ≥ 0 (0 disables) | `10` |
| `duplicate_effect` | `DENY` \| `REQUIRE_APPROVAL` | `REQUIRE_APPROVAL` |

### user
| Field | Type | Default |
|---|---|---|
| `daily_limit_minor` | int ≥ 0 | required |
| `approval_threshold_minor` | int ≥ 0 | required |
| `max_transaction_minor` | int ≥ 1 or null | null |

### merchant (optional document)
| Field | Type | Default |
|---|---|---|
| `max_transaction_minor` | int ≥ 1 or null | null |
| `allowed_categories` | list[str] or null | null (any) |
| `allowed_agents` | list[str] or null | null (any) |
| `approval_required_above_minor` | int ≥ 0 or null | null |

### agent
| Field | Type | Default |
|---|---|---|
| `enabled` | bool | `true` |
| `max_transaction_minor` | int ≥ 0 | required |
| `daily_limit_minor` | int ≥ 0 | required |
| `allowed_merchants` | list[str] or null | null (any) |
| `approval_required_above_minor` | int ≥ 0 or null | null |

## Demo configuration at a glance (USD)

| Limit | Value |
|---|---|
| Org daily budget (shared) | $500 |
| User approval threshold / daily limit | $50 / $500 |
| TechMart max agent purchase | $250 (peripherals, displays, audio) |
| DataHub max agent purchase | $100 (datasets, research agent only) |
| Shopping agent | $150 per transaction, $250/day, any merchant (org approval list still applies) |
| Research agent | $100 per transaction, $150/day, DataHub only |
| Procurement agent | $250 per transaction, $300/day, TechMart only |

The agent daily caps add up to $700, more than the $500 org budget. That's deliberate: it demonstrates cross-agent contention for the shared budget.

## Reason codes

### DENY
| Code | Source | Meaning |
|---|---|---|
| `SYSTEM_POLICY_MISSING`, `ORG_POLICY_MISSING`, `USER_POLICY_MISSING`, `AGENT_POLICY_MISSING` | presence | Required document absent (fail closed) |
| `<SCOPE>_POLICY_INVALID` | schema | Document fails validation (fail closed) |
| `POLICY_EVALUATION_ERROR` | system | Unexpected error during evaluation (fail closed) |
| `INVALID_AMOUNT` | system | Amount ≤ 0 |
| `CURRENCY_NOT_SUPPORTED` | system | Currency differs from the settlement currency |
| `ACTION_NOT_ALLOWED` | system | Action type not permitted |
| `SYSTEM_TRANSACTION_LIMIT` | system | Above the hard cap |
| `AGENT_INACTIVE` | system | Agent disabled |
| `MERCHANT_UNKNOWN` | system | Merchant does not exist |
| `ORDER_NOT_FOUND` | system | Order does not exist |
| `ORDER_MISMATCH` | system | Claimed order/merchant/amount/currency differ from the authoritative order |
| `ORDER_NOT_PAYABLE` | system | Order not in CREATED, PENDING, FAILED or CANCELLED |
| `DUPLICATE_ORDER_AUTHORIZATION` | system | Order already has an active or completed authorization |
| `MERCHANT_NOT_APPROVED` | org | Merchant not on the approved list |
| `ORG_TRANSACTION_LIMIT` | org | Above org per-transaction limit |
| `POSSIBLE_DUPLICATE_PAYMENT` | org | Same merchant + amount within the window (when `duplicate_effect=DENY`) |
| `USER_TRANSACTION_LIMIT` | user | Above user per-transaction limit |
| `MERCHANT_TRANSACTION_LIMIT` | merchant | Above merchant max |
| `MERCHANT_AGENT_NOT_ALLOWED` | merchant | Agent not accepted by merchant |
| `MERCHANT_CATEGORY_NOT_ALLOWED` | merchant | Order contains a disallowed category |
| `AGENT_POLICY_DISABLED` | agent | Agent spending authority disabled |
| `AGENT_TRANSACTION_LIMIT` | agent | Above agent per-transaction limit |
| `AGENT_MERCHANT_NOT_ALLOWED` | agent | Merchant not allowed for this agent |
| `BUDGET_EXCEEDED` | budget | Not enough headroom in `org:global`, `user:<id>` or `agent:<id>` |

### REQUIRE_APPROVAL
| Code | Source |
|---|---|
| `USER_APPROVAL_THRESHOLD` | user |
| `MERCHANT_APPROVAL_REQUIRED` | merchant |
| `AGENT_APPROVAL_REQUIRED` | agent |
| `POSSIBLE_DUPLICATE_PAYMENT` | org (when `duplicate_effect=REQUIRE_APPROVAL`) |

### ALLOW
| Code | Meaning |
|---|---|
| `WITHIN_POLICY` | No restriction triggered |
