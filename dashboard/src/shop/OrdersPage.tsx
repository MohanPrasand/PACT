import { useState } from "react";
import { usePoll } from "../lib/usePoll";
import { formatMoney } from "../lib/money";
import { StatusBadge } from "../shared/StatusBadge";
import { Card, Empty, ErrorNote, Spinner } from "../shared/ui";
import { api, isAgentRef } from "./api";
import { myCartId } from "./myCart";

type Filter = "all" | "mine" | "agents";

export function OrdersPage() {
  const { data, error, loading } = usePoll(api.orders, 3000);
  const [filter, setFilter] = useState<Filter>("all");
  const me = myCartId();
  const rows = (data ?? []).filter((o) => filter === "all" || (filter === "mine" ? o.buyer_ref === me : isAgentRef(o.buyer_ref)));
  const tab = (f: Filter, label: string) => (
    <button key={f} onClick={() => setFilter(f)} className={`rounded-md px-3 py-1.5 text-sm ${filter === f ? "bg-teal-700 text-white" : "bg-white text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50"}`}>{label}</button>
  );
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="mr-3 text-lg font-semibold text-slate-900">Orders</h1>
        {tab("all", "All")}{tab("mine", "My orders")}{tab("agents", "Agent orders")}
      </div>
      <ErrorNote error={error} />
      {loading && !data && <Spinner />}
      {data && rows.length === 0 && <Card><Empty>{filter === "mine" ? "You have not placed any orders yet." : "No orders to show."}</Empty></Card>}
      {rows.length > 0 && (
        <Card>
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase text-slate-400">
              <tr><th className="pb-2">Order</th><th className="pb-2">Merchant</th><th className="pb-2">Items</th><th className="pb-2">Buyer</th><th className="pb-2 text-right">Total</th><th className="pb-2 pl-4">Status</th><th className="pb-2">Updated</th></tr>
            </thead>
            <tbody>
              {rows.map((o) => (
                <tr key={o.order_id} className="border-t border-slate-100 align-top">
                  <td className="py-2"><a className="font-mono text-xs text-teal-700 hover:underline" href={`#/shop/orders/${o.order_id}`}>{o.order_id}</a></td>
                  <td className="py-2">{o.merchant_name ?? o.merchant_id}</td>
                  <td className="py-2 text-slate-600">{o.items.map((i) => `${i.quantity}× ${i.name}`).join(", ")}</td>
                  <td className="py-2 text-xs text-slate-500">{o.buyer_ref === me ? "me" : o.buyer_ref || "-"}</td>
                  <td className="py-2 text-right font-medium">{formatMoney(o.total_minor, o.currency)}</td>
                  <td className="py-2 pl-4">
                    <StatusBadge status={o.payment_status.toLowerCase()} label={o.payment_status} />
                    {(o.payment_status === "CREATED" || o.payment_status === "PENDING") && !isAgentRef(o.buyer_ref) && (
                      <a className="mt-1 block text-xs text-teal-700 underline" href={`#/shop/orders/${o.order_id}`}>Continue to payment</a>
                    )}
                  </td>
                  <td className="py-2 text-xs text-slate-500">{new Date(o.updated_at).toLocaleTimeString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
