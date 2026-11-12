import { useState } from "react";
import { usePoll } from "../lib/usePoll";
import { formatMoney } from "../lib/money";
import { Button, Card, ErrorNote, Spinner } from "../shared/ui";
import { api } from "./api";
import { myCartId } from "./myCart";
import { ProductArt } from "./ProductArt";

export function ProductPage({ id }: { id: string }) {
  const { data: p, error, loading } = usePoll(() => api.product(id), 0, undefined, [id]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<Error | null>(null);
  const [added, setAdded] = useState<number | null>(null);

  async function add() {
    setBusy(true);
    setErr(null);
    try {
      const cart = await api.cartAdd(myCartId(), id, 1);
      setAdded(cart.item_count);
    } catch (e) {
      setErr(e as Error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <a href="#/shop" className="text-sm text-teal-700 hover:underline">← Catalogue</a>
      <ErrorNote error={error} />
      {loading && !p && <Spinner />}
      {p && p.product_id === id && (
        <Card>
          <div className="flex flex-col gap-6 md:flex-row">
            <ProductArt category={p.category} className="h-56 w-full rounded-lg md:w-72" />
            <div className="flex-1 space-y-3">
              <div className="text-xs uppercase tracking-wide text-slate-400">{p.category} · {p.merchant_id}</div>
              <h1 className="text-xl font-semibold text-slate-900">{p.name}</h1>
              <p className="text-sm text-slate-600">{p.description}</p>
              <div className="text-2xl font-semibold">{formatMoney(p.price_minor, p.currency)}</div>
              <div className="text-sm text-slate-500">{p.rating.toFixed(1)} ★ · {p.stock > 0 ? `${p.stock} in stock` : "Out of stock"}</div>
              <div className="flex items-center gap-3 pt-2">
                <Button onClick={add} disabled={busy || p.stock < 1}>{busy ? "Adding…" : "Add to cart"}</Button>
                {added !== null && (
                  <span className="text-sm text-emerald-700">Added. <a className="underline" href="#/shop/cart/my">View cart ({added})</a></span>
                )}
              </div>
              <ErrorNote error={err} />
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}
