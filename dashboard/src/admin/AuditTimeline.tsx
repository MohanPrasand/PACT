import { useState } from "react";
import type { AuditEvent } from "./types";
import { fmtTime, redact } from "./util";

const BAD = new Set(["REPLAY_REJECTED", "EXECUTION_REJECTED"]);
const WARN = new Set(["DENIED", "GRANT_REVOKED", "GRANT_EXPIRED", "EXPIRED", "BUDGET_RESERVATION_FAILED", "EXECUTION_FAILED", "PROVIDER_TIMEOUT", "CAPTURE_MISMATCH"]);

export function eventTone(t: string): string {
  if (BAD.has(t)) return "border-red-300 bg-red-50";
  if (WARN.has(t)) return "border-amber-200 bg-amber-50";
  return "border-slate-200 bg-white";
}

export function AuditRow({ ev, showRequest }: { ev: AuditEvent; showRequest?: boolean }) {
  const [open, setOpen] = useState(false);
  const bad = BAD.has(ev.event_type);
  return (
    <li className={`rounded-md border px-3 py-2 text-sm ${eventTone(ev.event_type)}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-xs text-slate-400">#{ev.seq}</span>
        <span className={`font-mono text-xs font-semibold ${bad ? "text-red-700" : "text-slate-800"}`}>
          {bad && "⚠ "}
          {ev.event_type}
        </span>
        <span className="text-xs text-slate-500">{ev.actor}</span>
        {showRequest && ev.request_id && (
          <a className="font-mono text-xs text-indigo-600 hover:underline" href={`#/admin/requests/${encodeURIComponent(ev.request_id)}`}>
            {ev.request_id}
          </a>
        )}
        <span className="ml-auto text-xs text-slate-500">{fmtTime(ev.ts)}</span>
        <button className="text-xs text-indigo-600 hover:underline" onClick={() => setOpen(!open)}>
          {open ? "hide" : "details"}
        </button>
      </div>
      {open && (
        <pre className="mt-2 max-h-64 overflow-auto rounded bg-slate-900 p-2 text-xs text-slate-100">
          {JSON.stringify(redact(ev.data), null, 2)}
          {"\n"}hash {ev.hash}
          {"\n"}prev {ev.prev_hash}
        </pre>
      )}
    </li>
  );
}

/** Chronological list (oldest first) for one request. */
export function AuditTimeline({ events }: { events: AuditEvent[]; currency?: string }) {
  if (events.length === 0) return <p className="text-sm text-slate-500">No events.</p>;
  return (
    <ol className="space-y-1.5">
      {events.map((e) => (
        <AuditRow key={e.seq} ev={e} />
      ))}
    </ol>
  );
}
