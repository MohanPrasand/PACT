import { shopApi } from "../lib/shopApi";
import type { Cart, Merchant, Order, OrderStatus, Product, WebCheckoutResult } from "./types";

export const AGENT_CARTS = ["ag_shopping", "ag_research", "ag_procurement"] as const;

export const api = {
  merchants: () => shopApi<{ merchants: Merchant[] }>("/merchant/merchants").then((r) => r.merchants),
  products: (query: { q?: string; category?: string; max_price?: string; merchant_id?: string }) =>
    shopApi<{ products: Product[] }>("/merchant/products", { query }).then((r) => r.products),
  product: (id: string) => shopApi<Product>(`/merchant/products/${encodeURIComponent(id)}`),
  cart: (id: string) => shopApi<Cart>(`/merchant/carts/${encodeURIComponent(id)}`),
  cartAdd: (id: string, productId: string, quantity = 1) =>
    shopApi<Cart>(`/merchant/carts/${encodeURIComponent(id)}/items`, { method: "POST", body: { product_id: productId, quantity } }),
  cartRemove: (id: string, productId: string) =>
    shopApi<Cart>(`/merchant/carts/${encodeURIComponent(id)}/items/${encodeURIComponent(productId)}`, { method: "DELETE" }),
  cartCheckout: (id: string) => shopApi<Order>(`/merchant/carts/${encodeURIComponent(id)}/checkout`, { method: "POST", body: { buyer_ref: id } }),
  orders: () => shopApi<{ orders: Order[] }>("/merchant/transactions", { query: { limit: 200 } }).then((r) => r.orders),
  orderStatus: (id: string) => shopApi<OrderStatus>(`/merchant/orders/${encodeURIComponent(id)}/status`),
  orderCheckout: (id: string, retry = false) =>
    shopApi<WebCheckoutResult>(`/merchant/orders/${encodeURIComponent(id)}/checkout`, { method: "POST", body: { retry } }),
};

export const isAgentRef = (buyerRef: string) => buyerRef.startsWith("ag_");
