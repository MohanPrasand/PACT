import type { ReactNode } from "react";

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
}

export function shortId(id: string): string {
  return id.length > 16 ? `${id.slice(0, 13)}…` : id;
}

export function rid(id: string): string {
  return `#/admin/requests/${encodeURIComponent(id)}`;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm text-slate-900 break-words">{children}</dd>
    </div>
  );
}

export const inputCls =
  "w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500";

/** Never render these keys, wherever they appear in an audit payload or response. */
const HIDDEN = /grant_token|token/i;
export function redact(v: unknown): unknown {
  if (Array.isArray(v)) return v.map(redact);
  if (v && typeof v === "object") {
    return Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, HIDDEN.test(k) ? "[hidden]" : redact(x)]));
  }
  return v;
}
