// Shapes returned by /admin/* (see pact_core/core.py, pact_core/runtime.py request_view, pact_core/policy/engine.py).
// Grant tokens are never part of an admin response and are never modelled here.

export type Effect = "ALLOW" | "REQUIRE_APPROVAL" | "DENY";

export interface Finding {
  source: string;
  policy_id: string;
  rule: string;
  effect: Effect;
  code: string;
  message: string;
  limit_minor?: number | null;
  observed_minor?: number | null;
}

export interface Decision {
  decision: Effect;
  reason_codes: string[];
  findings: Finding[];
  policies_evaluated: { id: string; version: number }[];
  applicable_limits: Record<string, number | null>;
  approval_required: boolean;
  approval_reasons: string[];
  policy_version: string;
}

export interface RequestExecution {
  provider: string | null;
  provider_label: string | null;
  provider_order_id: string | null;
  provider_status: string | null;
  capture_id: string | null;
  approval_url: string | null;
  failure_reason: string | null;
}

export interface RequestView {
  request_id: string;
  state: string;
  agent_id: string;
  user_id: string;
  merchant_id: string;
  order_id: string;
  action_type: string;
  amount_minor: number;
  amount: string;
  currency: string;
  description: string;
  request_hash: string;
  decision: Decision | null;
  policy_version: string | null;
  approval_expires_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  grant: { grant_id: string; expires_at: string; consumed: boolean; revoked: boolean } | null;
  execution: RequestExecution;
  created_at: string;
  updated_at: string;
  next_action: string;
}

export interface OrderItem {
  product_id: string;
  name: string;
  category: string;
  quantity: number;
  unit_price_minor: number;
}

export interface OrderSummary {
  order_id: string;
  merchant_id: string;
  merchant_name: string;
  buyer_ref: string;
  items: OrderItem[];
  total_minor: number;
  total: string;
  currency: string;
  payment_status: string;
}

export interface Reservation {
  scope: string;
  period: string;
  amount_minor: number;
  status: string;
}

export interface AuditEvent {
  seq: number;
  ts: string;
  event_type: string;
  actor: string;
  request_id: string | null;
  data: Record<string, unknown>;
  hash: string;
  prev_hash: string;
}

export interface RequestDetail extends RequestView {
  order_snapshot: OrderSummary | null;
  reservations: Reservation[];
  audit: AuditEvent[];
  merchant_name: string | null;
  order_current: OrderSummary | null;
}

export interface BudgetScope {
  scope: string;
  period: string;
  limit_minor: number;
  reserved_minor: number;
  settled_minor: number;
  refunded_minor: number;
  available_minor: number;
}

export interface AgentRow {
  agent_id: string;
  name: string;
  description: string;
  user_id: string;
  active: boolean;
  created_at: string;
  budget: BudgetScope | null;
}

export interface AuditVerify {
  valid: boolean;
  events_checked: number;
  broken_at_seq: number | null;
  reason: string | null;
  head_seq: number | null;
  head_hash: string | null;
}

export interface Overview {
  currency: string;
  provider: string;
  budgets: BudgetScope[];
  agents: AgentRow[];
  requests_by_state: Record<string, number>;
  pending_approvals: number;
  recent: RequestView[];
  audit: AuditVerify;
}

export interface Policy {
  id: string;
  scope: "system" | "org" | "user" | "merchant" | "agent";
  subject: string;
  rules: Record<string, unknown>;
  version: number;
  updated_at: string;
  updated_by: string | null;
}
