/** Shapes of the public `/merchant/*` API (see merchant_lab/service.py and pact_server/storefront.py). */
export type PaymentStatus = "CREATED" | "PENDING" | "APPROVED" | "CAPTURED" | "FAILED" | "CANCELLED";

export interface Merchant {
  merchant_id: string;
  name: string;
  description: string;
}

export interface Product {
  product_id: string;
  merchant_id: string;
  name: string;
  description: string;
  category: string;
  price_minor: number;
  currency: string;
  price: string;
  stock: number;
  rating: number;
}

export interface CartLine {
  product_id: string;
  name: string;
  merchant_id: string;
  quantity: number;
  unit_price_minor: number;
  line_total_minor: number;
  currency: string;
}

export interface Cart {
  cart_id: string;
  items: CartLine[];
  item_count: number;
  total_minor: number;
  currency: string | null;
}

export interface OrderLine {
  product_id: string;
  name: string;
  category: string;
  quantity: number;
  unit_price_minor: number;
}

export interface Order {
  order_id: string;
  merchant_id: string;
  merchant_name: string | null;
  buyer_ref: string;
  items: OrderLine[];
  total_minor: number;
  total: string;
  currency: string;
  payment_status: PaymentStatus;
  authorization_id: string | null;
  provider_ref: string | null;
  created_at: string;
  updated_at: string;
}

export type Decision = "ALLOW" | "REQUIRE_APPROVAL" | "DENY";

export interface Authorization {
  request_id: string;
  agent_id: string;
  state: string;
  decision: Decision | null;
  reason_codes: string[];
  approval_pending: boolean;
  decided_by: string | null;
  decided_at: string | null;
  grant: { issued: boolean; consumed: boolean } | null;
  provider: string | null;
  provider_label: string | null;
  provider_order_id: string | null;
  capture_id: string | null;
  checkout_url: string | null;
  failure_reason: string | null;
  terminal: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface OrderStatus {
  order: Order;
  payment_status: PaymentStatus;
  authorization: Authorization | null;
}

export interface WebCheckoutResult {
  order_id: string;
  authorization: Authorization;
  state: string;
  request_id: string;
  checkout_url: string | null;
  reason_codes: string[];
  approval_pending: boolean;
}
