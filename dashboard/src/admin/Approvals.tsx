import { useState } from "react";
import { formatMoney } from "../lib/money";
import { usePoll } from "../lib/usePoll";
import { StatusBadge } from "../shared/StatusBadge";
import { Card, Empty, ErrorNote, Spinner } from "../shared/ui";
import { api } from "./api";
import { RequestPanel } from "./RequestPanel";
import { fmtTime, shortId } from "./util";

const FILTERS = [
  { v: "PENDING_APPROVAL", l: "Pending approval" },
  { v: "", l: "All" },
  { v: "EXECUTING", l: "Executing" },
  { v: "UNKNOWN", l: "Unknown" },
  { v: "EXECUTED", l: "Executed" },
  { v: "DENIED", l: "Denied" },
  { v: "FAILED", l: "Failed" },
];

export function Approvals({ route }: { route: string }) {
  const q = new URLSearchParams(route.split("?")[1] ?? "");
  const [state, setState] = useState(q.get("state") ?? "PENDING_APPROVAL");
  const [sel, setSel] = useState<string | null>(null);
  const { data, error, loading, refresh } = usePoll(() => api.list(state || undefined), 3000, undefined, [state]);
  const rows = data?.authorizations ?? [];
  const selected = sel ?? (state === "PENDING_APPROVAL" ? rows[0]?.request_id ?? null : null);

  return (
    <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
      <Card
        title="Approval Centre"
        actions={<span className="text-xs text-slate-400">auto-refresh 3s</span>}
      >
        <div className="mb-3 flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button
              key={f.v}
              onClick={() => {
                setState(f.v);
                setSel(null);
              }}
              className={`rounded-full px-2.5 py-1 text-xs ${state === f.v ? "bg-slate-900 text-white" : "bg-slate-100 text-slate-700 hover:bg-slate-200"}`}
            >
              {f.l}
            </button>
          ))}
        </div>
        <ErrorNote error={error} />
        {loading && !data ? (
          <Spinner />
        ) : rows.length === 0 ? (
          <Empty>{state === "PENDING_APPROVAL" ? "Nothing waiting for approval." : "No requests."}</Empty>
        ) : (
          <ul className="max-h-[calc(100vh-14rem)] space-y-1.5 overflow-auto">
            {rows.map((r) => (
              <li key={r.request_id}>
                <button
                  onClick={() => setSel(r.request_id)}
                  className={`w-full rounded-md border px-3 py-2 text-left ${selected === r.request_id ? "border-indigo-400 bg-indigo-50" : "border-slate-200 hover:bg-slate-50"}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-sm font-semibold">{formatMoney(r.amount_minor, r.currency)}</span>
                    <StatusBadge status={r.state} />
                  </div>
                  <div className="text-xs text-slate-600">
                    {r.agent_id} → {r.merchant_id}
                  </div>
                  <div className="flex justify-between text-xs text-slate-400">
                    <span className="font-mono">{shortId(r.request_id)}</span>
                    <span>{fmtTime(r.created_at)}</span>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card>
        {selected ? <RequestPanel key={selected} requestId={selected} onChanged={refresh} /> : <Empty>Select a request.</Empty>}
      </Card>
    </div>
  );
}
