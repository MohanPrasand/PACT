import { useEffect, useMemo, useState } from "react";
import { usePoll } from "../lib/usePoll";
import { formatMoney } from "../lib/money";
import { Card, Empty, ErrorNote, Spinner } from "../shared/ui";
import { api } from "./api";
import { ProductArt } from "./ProductArt";
import type { Product } from "./types";

const PRICE_RE = /^\d+(\.\d{1,2})?$/;

export function Catalogue() {
  const [q, setQ] = useState("");
  const [dq, setDq] = useState("");
  const [category, setCategory] = useState("");
  const [maxPrice, setMaxPrice] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDq(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);

  const priceOk = maxPrice === "" || PRICE_RE.test(maxPrice.trim());
  const merchants = usePoll(api.merchants, 0);
  const all = usePoll(() => api.products({}), 0);
  const res = usePoll(
    () => api.products({ q: dq || undefined, category: category || undefined, max_price: priceOk && maxPrice ? maxPrice.trim() : undefined }),
    0, undefined, [dq, category, maxPrice, priceOk],
  );

  const categories = useMemo(() => [...new Set((all.data ?? []).map((p) => p.category))].sort(), [all.data]);
  const groups = useMemo(() => {
    const by = new Map<string, Product[]>();
    for (const p of res.data ?? []) by.set(p.merchant_id, [...(by.get(p.merchant_id) ?? []), p]);
    const order = (merchants.data ?? []).map((m) => m.merchant_id);
    return [...by.entries()].sort((a, b) => (order.indexOf(a[0]) + 1 || 99) - (order.indexOf(b[0]) + 1 || 99));
  }, [res.data, merchants.data]);
  const nameOf = (id: string) => merchants.data?.find((m) => m.merchant_id === id);
  const filtered = dq !== "" || category !== "" || maxPrice !== "";

  const field = "rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm focus:border-teal-600 focus:outline-none focus:ring-1 focus:ring-teal-600";
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex-1 min-w-48 text-xs text-slate-500">Search
          <input className={`${field} mt-1 block w-full`} placeholder="keyboard, dataset…" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <label className="text-xs text-slate-500">Category
          <select className={`${field} mt-1 block`} value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {categories.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <label className="text-xs text-slate-500">Max price (USD)
          <input className={`${field} mt-1 block w-32 ${priceOk ? "" : "border-red-400"}`} inputMode="decimal" placeholder="100.00" value={maxPrice} onChange={(e) => setMaxPrice(e.target.value)} />
        </label>
        {filtered && (
          <button className="pb-2 text-sm text-teal-700 hover:underline" onClick={() => { setQ(""); setCategory(""); setMaxPrice(""); }}>Clear</button>
        )}
      </div>
      {!priceOk && <p className="text-xs text-red-600">Enter a price like 100 or 100.00.</p>}
      <ErrorNote error={res.error} />
      {res.loading && !res.data && <Spinner label="Loading products…" />}
      {res.data && groups.length === 0 && (
        <Card><Empty>{filtered ? "No products match your filters." : "The catalogue is empty. Run `pact init` to seed Merchant Lab."}</Empty></Card>
      )}
      {groups.map(([mid, products]) => (
        <section key={mid}>
          <div className="mb-2 flex items-baseline gap-3">
            <h2 className="text-base font-semibold text-slate-800">{nameOf(mid)?.name ?? mid}</h2>
            <span className="text-xs text-slate-500">{nameOf(mid)?.description}</span>
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4">
            {products.map((p) => (
              <a key={p.product_id} href={`#/shop/products/${encodeURIComponent(p.product_id)}`}
                className="group overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm transition hover:border-teal-500 hover:shadow">
                <ProductArt category={p.category} className="h-24 w-full" />
                <div className="space-y-1 p-3">
                  <div className="text-xs uppercase tracking-wide text-slate-400">{p.category}</div>
                  <div className="text-sm font-medium leading-snug text-slate-800 group-hover:text-teal-700">{p.name}</div>
                  <div className="flex items-center justify-between pt-1">
                    <span className="text-sm font-semibold text-slate-900">{formatMoney(p.price_minor, p.currency)}</span>
                    <span className="text-xs text-slate-500">{p.rating.toFixed(1)} ★</span>
                  </div>
                  <div className={`text-xs ${p.stock > 0 ? "text-slate-500" : "text-red-600"}`}>{p.stock > 0 ? `${p.stock} in stock` : "Out of stock"}</div>
                </div>
              </a>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
