import { useState } from "react";
import { usePoll } from "../lib/usePoll";
import { Card, Empty, ErrorNote, Spinner } from "../shared/ui";
import { api } from "./api";
import { AuditRow } from "./AuditTimeline";
import { inputCls } from "./util";

const TYPES = [
  "REQUEST_CREATED", "POLICY_EVALUATED", "BUDGET_RESERVED", "BUDGET_RESERVATION_FAILED", "APPROVAL_REQUESTED", "APPROVED", "DENIED",
  "GRANT_ISSUED", "GRANT_DELIVERED", "GRANT_CONSUMED", "GRANT_REVOKED", "GRANT_EXPIRED", "EXECUTION_STARTED", "EXECUTION_REJECTED",
  "EXECUTION_FAILED", "REPLAY_REJECTED", "PROVIDER_ORDER_CREATED", "PROVIDER_TIMEOUT", "PAYMENT_CAPTURED", "CAPTURE_MISMATCH",
  "CAPTURE_NOT_COMPLETED", "CAPTURE_AFTER_TERMINAL_STATE", "CANCEL_REQUESTED", "RECONCILED", "MERCHANT_SYNC_FAILED", "POLICY_UPDATED",
];

export function VerifyBadge() {
  const { data, error } = usePoll(api.verify, 15000);
  if (error) return <span className="text-xs text-red-700">verify failed</span>;
  if (!data) return <span className="text-xs text-slate-400">verifying…</span>;
  return data.valid ? (
    <span className="rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-medium text-emerald-800 ring-1 ring-inset ring-emerald-200">
      Chain verified · {data.events_checked} events
    </span>
  ) : (
    <span className="rounded-full bg-red-100 px-2.5 py-1 text-xs font-semibold text-red-800 ring-1 ring-inset ring-red-300">
      CHAIN BROKEN at #{data.broken_at_seq}: {data.reason}
    </span>
  );
}

export function Audit() {
  const [reqId, setReqId] = useState("");
  const [type, setType] = useState("");
  const [applied, setApplied] = useState({ reqId: "", type: "" });
  const { data, error, loading, refresh } = usePoll(() => api.audit(applied.reqId.trim() || undefined, applied.type || undefined), 0, undefined, [applied.reqId, applied.type]);
  const events = data?.events ?? [];

  return (
    <Card title="Audit Explorer" actions={<VerifyBadge />}>
      <form
        className="mb-4 flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied({ reqId, type });
          refresh();
        }}
      >
        <label className="text-xs text-slate-500">
          Request id
          <input className={`${inputCls} mt-1 w-64`} value={reqId} onChange={(e) => setReqId(e.target.value)} placeholder="auth_…" />
        </label>
        <label className="text-xs text-slate-500">
          Event type
          <select className={`${inputCls} mt-1 w-60`} value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">All</option>
            {TYPES.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </select>
        </label>
        <button className="rounded-md bg-slate-900 px-3 py-1.5 text-sm text-white hover:bg-slate-700">Filter</button>
        <button
          type="button"
          className="rounded-md px-3 py-1.5 text-sm text-slate-600 ring-1 ring-inset ring-slate-300 hover:bg-slate-50"
          onClick={() => {
            setReqId("");
            setType("");
            setApplied({ reqId: "", type: "" });
          }}
        >
          Clear
        </button>
        <button type="button" className="text-sm text-indigo-600 hover:underline" onClick={refresh}>
          Refresh
        </button>
      </form>
      <ErrorNote error={error} />
      {loading && !data ? (
        <Spinner />
      ) : events.length === 0 ? (
        <Empty>No matching events.</Empty>
      ) : (
        <>
          <p className="mb-2 text-xs text-slate-500">
            {events.length} events, newest first. Replay and execution rejections are highlighted.
          </p>
          <ol className="space-y-1.5">
            {events.map((e) => (
              <AuditRow key={e.seq} ev={e} showRequest />
            ))}
          </ol>
        </>
      )}
    </Card>
  );
}
