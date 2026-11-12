import { formatMoney } from "../lib/money";
import { usePoll } from "../lib/usePoll";
import { StatusBadge } from "../shared/StatusBadge";
import { Card, Empty, ErrorNote, Spinner } from "../shared/ui";
import { api } from "./api";
import { fmtTime, rid, shortId } from "./util";

export function Overview() {
  const { data, error, loading } = usePoll(api.overview, 5000);
  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote error={error} />;
  const cur = data.currency;
  const org = data.budgets.find((b) => b.scope.startsWith("org:"));
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="grid gap-4 md:grid-cols-4">
        <a href="#/admin/approvals" className={`rounded-lg border p-4 shadow-sm ${data.pending_approvals ? "border-amber-300 bg-amber-50" : "border-slate-200 bg-white"}`}>
          <div className="text-xs uppercase text-slate-500">Pending approvals</div>
          <div className="text-3xl font-semibold">{data.pending_approvals}</div>
        </a>
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <div className="text-xs uppercase text-slate-500">Org budget available</div>
          <div className="text-3xl font-semibold">{org ? formatMoney(org.available_minor, cur) : "–"}</div>
          {org && <div className="text-xs text-slate-500">of {formatMoney(org.limit_minor, cur)} today</div>}
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
          <div className="text-xs uppercase text-slate-500">Provider</div>
          <div className="text-lg font-semibold">{data.provider === "paypal" ? "PayPal Sandbox" : "Simulation (not PayPal)"}</div>
        </div>
        <a href="#/admin/audit" className={`rounded-lg border p-4 shadow-sm ${data.audit.valid ? "border-emerald-200 bg-emerald-50" : "border-red-300 bg-red-50"}`}>
          <div className="text-xs uppercase text-slate-500">Audit chain</div>
          <div className={`text-lg font-semibold ${data.audit.valid ? "text-emerald-800" : "text-red-800"}`}>{data.audit.valid ? "Verified" : "BROKEN"}</div>
          <div className="text-xs text-slate-500">{data.audit.events_checked} events</div>
        </a>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Requests by state">
          {Object.keys(data.requests_by_state).length === 0 ? (
            <Empty>No requests yet.</Empty>
          ) : (
            <div className="flex flex-wrap gap-3">
              {Object.entries(data.requests_by_state).map(([s, n]) => (
                <a key={s} href={`#/admin/approvals?state=${s}`} className="flex items-center gap-2">
                  <StatusBadge status={s} /> <span className="text-sm font-semibold">{n}</span>
                </a>
              ))}
            </div>
          )}
        </Card>
        <Card title="Budgets (today)">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-slate-500">
                <th className="py-1">Scope</th>
                <th className="text-right">Reserved</th>
                <th className="text-right">Settled</th>
                <th className="text-right">Available</th>
                <th className="text-right">Limit</th>
              </tr>
            </thead>
            <tbody>
              {data.budgets.map((b) => (
                <tr key={b.scope} className="border-t border-slate-100">
                  <td className="py-1 font-mono text-xs">{b.scope}</td>
                  <td className="text-right">{formatMoney(b.reserved_minor, cur)}</td>
                  <td className="text-right">{formatMoney(b.settled_minor, cur)}</td>
                  <td className={`text-right ${b.available_minor < 0 ? "text-red-700" : ""}`}>{formatMoney(b.available_minor, cur)}</td>
                  <td className="text-right text-slate-500">{formatMoney(b.limit_minor, cur)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <Card title="Agents">
        <table className="w-full text-sm">
          <tbody>
            {data.agents.map((a) => (
              <tr key={a.agent_id} className="border-t border-slate-100 first:border-0">
                <td className="py-1.5">
                  <div className="font-medium">{a.name}</div>
                  <div className="font-mono text-xs text-slate-500">{a.agent_id}</div>
                </td>
                <td>{a.active ? <StatusBadge status="ALLOW" label="active" /> : <StatusBadge status="REVOKED" label="disabled" />}</td>
                <td className="text-right text-slate-600">
                  {a.budget ? `${formatMoney(a.budget.available_minor, cur)} left of ${formatMoney(a.budget.limit_minor, cur)}` : "–"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Recent requests" actions={<a className="text-xs text-indigo-600 hover:underline" href="#/admin/approvals">All requests</a>}>
        {data.recent.length === 0 ? (
          <Empty>No requests yet.</Empty>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {data.recent.map((r) => (
                <tr key={r.request_id} className="border-t border-slate-100 first:border-0">
                  <td className="py-1.5">
                    <a className="font-mono text-xs text-indigo-600 hover:underline" href={rid(r.request_id)}>
                      {shortId(r.request_id)}
                    </a>
                  </td>
                  <td>{r.agent_id}</td>
                  <td>{r.merchant_id}</td>
                  <td className="text-right">{formatMoney(r.amount_minor, r.currency)}</td>
                  <td>
                    <StatusBadge status={r.state} />
                  </td>
                  <td className="text-right text-xs text-slate-500">{fmtTime(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
