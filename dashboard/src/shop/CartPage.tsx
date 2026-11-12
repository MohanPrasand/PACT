import { useEffect, useRef, useState } from "react";
import { navigate } from "../lib/useHashRoute";
import { usePoll } from "../lib/usePoll";
import { formatMoney } from "../lib/money";
import { Button, Card, Empty, ErrorNote, Spinner } from "../shared/ui";
import { AGENT_CARTS, api } from "./api";
import { myCartId, resolveCartId } from "./myCart";

export function CartPage({ routeId }: { routeId: string }) {
  const cartId = resolveCartId(routeId);
  const mine = cartId === myCartId();
  const { data, error, loading, refresh } = usePoll(() => api.cart(cartId), 2000, undefined, [cartId]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<Error | null>(null);
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const prev = useRef<{ id: string; qty: Map<string, number> } | null>(null);

  useEffect(() => {
    if (!data || data.cart_id !== cartId) return;
    const qty = new Map(data.items.map((i) => [i.product_id, i.quantity]));
    const p = prev.current;
    if (p && p.id === cartId) {
      const changed = data.items.filter((i) => (p.qty.get(i.product_id) ?? 0) < i.quantity).map((i) => i.product_id);
      if (changed.length) {
        setFlash(new Set(changed));
        setTimeout(() => setFlash(new Set()), 3000);
      }
    }
    prev.current = { id: cartId, qty };
  }, [data, cartId]);

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      refresh();
    } catch (e) {
      setErr(e as Error);
      refresh();
    } finally {
      setBusy(false);
    }
  }

  const setQty = (pid: string, from: number, to: number) =>
    run(async () => {
      if (to > from) await api.cartAdd(cartId, pid, to - from);
      else {
        await api.cartRemove(cartId, pid);
        if (to > 0) await api.cartAdd(cartId, pid, to);
      }
    });

  async function checkout() {
    setBusy(true);
    setErr(null);
    try {
      const order = await api.cartCheckout(cartId);
      navigate(`/shop/orders/${encodeURIComponent(order.order_id)}?start=1`);
    } catch (e) {
      setErr(e as Error);
      setBusy(false);
    }
  }

  const cart = data && data.cart_id === cartId ? data : null;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-slate-900">Cart</h1>
        <select aria-label="Cart" value={mine ? "my" : cartId} onChange={(e) => navigate(`/shop/cart/${e.target.value}`)}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm">
          <option value="my">My cart</option>
          {AGENT_CARTS.map((a) => <option key={a} value={a}>{a} (agent)</option>)}
        </select>
        <span className="flex items-center gap-1.5 text-xs text-slate-500"><span className="h-2 w-2 animate-pulse rounded-full bg-emerald-500" /> live, updates every 2s</span>
      </div>
      {!mine && <p className="text-sm text-slate-500">Watching an agent's cart. Only you can check out your own cart.</p>}
      <ErrorNote error={error} />
      <ErrorNote error={err} />
      {loading && !cart && <Spinner />}
      {cart && cart.items.length === 0 && (
        <Card><Empty>{mine ? <>Your cart is empty. <a className="text-teal-700 underline" href="#/shop">Browse the catalogue</a>.</> : "This agent's cart is empty. Items it adds appear here live."}</Empty></Card>
      )}
      {cart && cart.items.length > 0 && (
        <Card>
          <table className="w-full text-base">
            <thead className="text-left text-xs uppercase text-slate-400">
              <tr><th className="pb-2">Item</th><th className="pb-2 text-right">Unit</th><th className="pb-2 text-center">Qty</th><th className="pb-2 text-right">Line total</th><th /></tr>
            </thead>
            <tbody>
              {cart.items.map((l) => (
                <tr key={l.product_id} className={`border-t border-slate-100 transition-colors duration-700 ${flash.has(l.product_id) ? "bg-emerald-100" : ""}`}>
                  <td className="py-2"><a className="font-medium text-slate-800 hover:text-teal-700" href={`#/shop/products/${l.product_id}`}>{l.name}</a>
                    <div className="text-xs text-slate-400">{l.merchant_id}</div></td>
                  <td className="py-2 text-right">{formatMoney(l.unit_price_minor, l.currency)}</td>
                  <td className="py-2 text-center">
                    {mine ? (
                      <span className="inline-flex items-center gap-2">
                        <button aria-label="Decrease" disabled={busy} className="h-6 w-6 rounded ring-1 ring-slate-300 hover:bg-slate-50 disabled:opacity-50" onClick={() => setQty(l.product_id, l.quantity, l.quantity - 1)}>−</button>
                        {l.quantity}
                        <button aria-label="Increase" disabled={busy} className="h-6 w-6 rounded ring-1 ring-slate-300 hover:bg-slate-50 disabled:opacity-50" onClick={() => setQty(l.product_id, l.quantity, l.quantity + 1)}>+</button>
                      </span>
                    ) : l.quantity}
                  </td>
                  <td className="py-2 text-right font-medium">{formatMoney(l.line_total_minor, l.currency)}</td>
                  <td className="py-2 pl-3 text-right">{mine && <button disabled={busy} className="text-xs text-red-600 hover:underline" onClick={() => run(() => api.cartRemove(cartId, l.product_id))}>Remove</button>}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-slate-200">
                <td colSpan={3} className="pt-3 text-right text-slate-500">Total ({cart.item_count} items)</td>
                <td className="pt-3 text-right text-xl font-semibold">{cart.currency ? formatMoney(cart.total_minor, cart.currency) : "mixed currencies"}</td>
                <td />
              </tr>
            </tfoot>
          </table>
          {mine && (
            <div className="mt-4 flex items-center justify-end gap-3">
              <span className="text-xs text-slate-500">Payment is authorised by PACT and may need your approval.</span>
              <Button onClick={checkout} disabled={busy}>{busy ? "Working…" : "Checkout & pay"}</Button>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
