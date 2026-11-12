import { useState } from "react";
import { formatMoney } from "../lib/money";
import { ApiError } from "../lib/errors";
import { usePoll } from "../lib/usePoll";
import { StatusBadge } from "../shared/StatusBadge";
import { Button, Card, ErrorNote, Spinner } from "../shared/ui";
import { api } from "./api";
import { AuditTimeline } from "./AuditTimeline";
import type { Finding } from "./types";
import { Field, fmtTime, inputCls, shortId } from "./util";

const TERMINAL = new Set(["DENIED", "EXECUTED", "FAILED", "EXPIRED", "REVOKED"]);
const EFFECT_TONE: Record<string, string> = {
  DENY: "border-red-200 bg-red-50 text-red-900",
  REQUIRE_APPROVAL: "border-amber-200 bg-amber-50 text-amber-900",
  ALLOW: "border-emerald-200 bg-emerald-50 text-emerald-900",
};

function FindingRow({ f, currency }: { f: Finding; currency: string }) {
  return (
    <li className={`rounded-md border px-3 py-2 text-sm ${EFFECT_TONE[f.effect] ?? "border-slate-200"}`}>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={f.effect} />
        <code className="text-xs font-semibold">{f.code}</code>
        <span className="text-xs opacity-70">
          {f.source} · {f.policy_id} · {f.rule}
        </span>
      </div>
      <p className="mt-1">{f.message}</p>
      {(f.limit_minor != null || f.observed_minor != null) && (
        <p className="mt-0.5 text-xs opacity-80">
          {f.observed_minor != null && <>Observed {formatMoney(f.observed_minor, currency)}</>}
          {f.limit_minor != null && f.observed_minor != null && " · "}
          {f.limit_minor != null && <>Limit {formatMoney(f.limit_minor, currency)}</>}
        </p>
      )}
    </li>
  );
}

/** Full request view + admin actions. Polls every 3s until the request reaches a terminal state. */
export function RequestPanel({ requestId, onChanged }: { requestId: string; onChanged?: () => void }) {
  const { data: d, error, loading, refresh } = usePoll(() => api.detail(requestId), 3000, (x) => TERMINAL.has(x.state), [requestId]);
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<Error | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  if (loading && !d) return <Spinner />;
  if (!d) return <ErrorNote error={error} />;

  const run = async (name: string, fn: () => Promise<unknown>, done: string) => {
    setBusy(name);
    setActionErr(null);
    setFlash(null);
    try {
      await fn();
      setFlash(done);
      refresh();
      onChanged?.();
    } catch (e) {
      setActionErr(e as Error);
      if (e instanceof ApiError && e.status === 409) refresh(); // reload what the server now says
    } finally {
      setBusy(null);
    }
  };

  const cur = d.currency;
  const snap = d.order_snapshot;
  const pending = d.state === "PENDING_APPROVAL";
  const canRevoke = ["PENDING_APPROVAL", "APPROVED", "GRANTED"].includes(d.state);
  const live = d.state === "EXECUTING" || d.state === "UNKNOWN";
  const conflict = actionErr instanceof ApiError && actionErr.status === 409;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="font-mono text-sm text-slate-700">{d.request_id}</h3>
        <StatusBadge status={d.state} />
        {d.decision && <StatusBadge status={d.decision.decision} />}
        <a className="ml-auto text-xs text-indigo-600 hover:underline" href={`#/admin/requests/${encodeURIComponent(d.request_id)}`}>
          Open full page
        </a>
      </div>

      <dl className="grid grid-cols-2 gap-x-6 gap-y-2 md:grid-cols-5">
        <Field label="Merchant">{d.merchant_name ?? d.merchant_id}</Field>
        <Field label="Amount">
          <span className="text-lg font-semibold">{formatMoney(d.amount_minor, cur)}</span> <span className="text-xs text-slate-500">{cur}</span>
        </Field>
        <Field label="Agent">{d.agent_id}</Field>
        <Field label="Order">
          <code className="text-xs">{shortId(d.order_id)}</code>
        </Field>
        <Field label="Expires">{fmtTime(d.approval_expires_at)}</Field>
      </dl>
      {d.description && <p className="text-sm text-slate-600">Agent says: {d.description}</p>}

      <div className="grid gap-4 lg:grid-cols-[1.15fr_1fr]">
        <div className="space-y-4">
      {d.decision && (
        <section>
          <h4 className="mb-2 text-sm font-semibold text-slate-800">Policy findings</h4>
          {d.decision.findings.length === 0 ? (
            <p className="text-sm text-slate-500">No findings; all policies passed.</p>
          ) : (
            <ul className="space-y-2">
              {d.decision.findings.map((f, i) => (
                <FindingRow key={i} f={f} currency={cur} />
              ))}
            </ul>
          )}
          {d.decision.reason_codes.length > 0 && (
            <p className="mt-2 text-xs text-slate-500">
              Reason codes: {d.decision.reason_codes.map((c) => <code key={c} className="mr-2 rounded bg-slate-100 px-1">{c}</code>)}
            </p>
          )}
          <p className="mt-1 text-xs text-slate-500">
            Policies: {d.decision.policies_evaluated.map((p) => `${p.id}@v${p.version}`).join(", ")}
          </p>
        </section>
      )}

      <section>
        <h4 className="mb-2 text-sm font-semibold text-slate-800">Order {snap ? `(snapshot at request time)` : ""}</h4>
        {snap ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
                <th className="py-1">Item</th>
                <th>Category</th>
                <th className="text-right">Qty</th>
                <th className="text-right">Unit</th>
              </tr>
            </thead>
            <tbody>
              {snap.items.map((it) => (
                <tr key={it.product_id} className="border-b border-slate-100">
                  <td className="py-1">{it.name}</td>
                  <td className="text-slate-500">{it.category}</td>
                  <td className="text-right">{it.quantity}</td>
                  <td className="text-right">{formatMoney(it.unit_price_minor, snap.currency)}</td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={3} className="pt-2 text-right text-xs uppercase text-slate-500">
                  Order total
                </td>
                <td className="pt-2 text-right font-semibold">{formatMoney(snap.total_minor, snap.currency)}</td>
              </tr>
            </tfoot>
          </table>
        ) : (
          <p className="text-sm text-slate-500">No order snapshot.</p>
        )}
        {d.order_current && snap && d.order_current.total_minor !== snap.total_minor && (
          <p className="mt-2 rounded border border-red-200 bg-red-50 px-2 py-1 text-xs text-red-800">
            The order has changed since this request was made (now {formatMoney(d.order_current.total_minor, d.order_current.currency)}). Approval will be refused.
          </p>
        )}
      </section>
        </div>
        <div className="space-y-4">
      <ErrorNote error={actionErr} />
      {conflict && (
        <div className="flex items-center gap-2 text-sm text-amber-800">
          The request changed on the server and has been reloaded; review it before trying again.
          <Button variant="ghost" onClick={refresh}>
            Reload
          </Button>
        </div>
      )}
      {flash && <p className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{flash}</p>}
      {pending && (
        <Card title="Decision">
          <div className="space-y-3">
            <input className={inputCls} value={note} maxLength={500} onChange={(e) => { setNote(e.target.value); setReason(e.target.value); }} placeholder="Note or denial reason (optional)" />
            <div className="grid grid-cols-2 gap-3">
              <Button className="py-2.5 text-base" disabled={busy !== null} onClick={() => run("approve", () => api.approve(d.request_id, d.request_hash, note), "Approved.")}>
                {busy === "approve" ? "Approving…" : "Approve"}
              </Button>
              <Button variant="danger" className="py-2.5 text-base" disabled={busy !== null} onClick={() => run("deny", () => api.deny(d.request_id, reason), "Denied.")}>
                {busy === "deny" ? "Denying…" : "Deny"}
              </Button>
            </div>
            <p className="text-xs text-slate-500">
              Approving binds exactly {formatMoney(d.amount_minor, cur)} to {d.merchant_name ?? d.merchant_id}; hash <code>{d.request_hash.slice(0, 12)}…</code>
            </p>
          </div>
        </Card>
      )}
      {(live || canRevoke) && (
        <div className="flex flex-wrap gap-2">
          {live && (
            <>
              <Button variant="ghost" disabled={busy !== null} onClick={() => run("reconcile", () => api.reconcile(d.request_id), "Reconciled with the provider.")}>
                {busy === "reconcile" ? "Reconciling…" : "Reconcile"}
              </Button>
              <Button
                variant="danger"
                disabled={busy !== null}
                onClick={() => window.confirm("Cancel this payment stuck in checkout? Funds are released unless the provider already captured.") && run("cancel", () => api.cancel(d.request_id), "Payment cancelled.")}
              >
                Cancel payment
              </Button>
            </>
          )}
          {canRevoke && !pending && (
            <Button variant="danger" disabled={busy !== null} onClick={() => window.confirm("Revoke this authorization?") && run("revoke", () => api.revoke(d.request_id), "Authorization revoked.")}>
              Revoke
            </Button>
          )}
        </div>
      )}

      <section>
        <h4 className="mb-2 text-sm font-semibold text-slate-800">Budget impact</h4>
        {d.reservations.length === 0 ? (
          <p className="text-sm text-slate-500">No active reservations.</p>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {d.reservations.map((r) => (
                <tr key={r.scope + r.period} className="border-b border-slate-100">
                  <td className="py-1 font-mono text-xs">{r.scope}</td>
                  <td className="text-slate-500">{r.period}</td>
                  <td className="text-right">{formatMoney(r.amount_minor, cur)}</td>
                  <td className="text-right">
                    <StatusBadge status={r.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {d.decision && (
          <details className="mt-2 text-xs text-slate-600">
            <summary className="cursor-pointer">Applicable limits</summary>
            <ul className="mt-1 grid grid-cols-1 gap-x-6 md:grid-cols-2">
              {Object.entries(d.decision.applicable_limits).map(([k, v]) => (
                <li key={k} className="flex justify-between border-b border-slate-100 py-0.5">
                  <span className="font-mono">{k}</span>
                  <span>{v == null ? "none" : formatMoney(v, cur)}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>
        </div>
      </div>

      {d.decided_by && (
        <p className="text-sm text-slate-600">
          Decided by <b>{d.decided_by}</b> at {fmtTime(d.decided_at)}
          {d.decision_note ? `: "${d.decision_note}"` : ""}
        </p>
      )}

      {(d.execution.provider || d.execution.failure_reason) && (
        <section>
          <h4 className="mb-2 text-sm font-semibold text-slate-800">Execution</h4>
          <dl className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Field label="Provider">{d.execution.provider_label ?? d.execution.provider ?? "–"}</Field>
            <Field label="Provider status">{d.execution.provider_status ?? "–"}</Field>
            <Field label="Provider order">{d.execution.provider_order_id ?? "–"}</Field>
            <Field label="Capture">{d.execution.capture_id ?? "–"}</Field>
            {d.execution.failure_reason && <Field label="Failure">{d.execution.failure_reason}</Field>}
            {d.grant && <Field label="Grant">{d.grant.consumed ? "consumed" : d.grant.revoked ? "revoked" : `valid until ${fmtTime(d.grant.expires_at)}`}</Field>}
          </dl>
        </section>
      )}

      <p className="text-xs text-slate-500">
        Request hash: <code className="break-all">{d.request_hash}</code>
      </p>

      <section>
        <h4 className="mb-2 text-sm font-semibold text-slate-800">Audit timeline</h4>
        <AuditTimeline events={d.audit} currency={cur} />
      </section>
    </div>
  );
}
