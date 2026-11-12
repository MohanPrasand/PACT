import { useEffect, useState } from "react";
import { ApiError } from "../lib/errors";
import { formatMoney } from "../lib/money";
import { usePoll } from "../lib/usePoll";
import { Button, Card, ErrorNote, Spinner } from "../shared/ui";
import { api } from "./api";
import type { Policy } from "./types";
import { fmtTime, inputCls } from "./util";

type Kind = "money" | "moneyOpt" | "int" | "bool" | "list" | "listOpt" | "enum";
interface FieldDef {
  key: string;
  label: string;
  kind: Kind;
  help?: string;
  options?: string[];
}

// Mirrors pact_core/policy/rules.py. The server remains the authority; its errors are shown inline.
const SCHEMA: Record<Policy["scope"], FieldDef[]> = {
  system: [
    { key: "currency", label: "Currency (ISO code)", kind: "enum", options: [] },
    { key: "hard_max_transaction_minor", label: "Hard max per transaction", kind: "money", help: "Approval can never override this." },
    { key: "allowed_actions", label: "Allowed actions", kind: "list" },
  ],
  org: [
    { key: "daily_budget_minor", label: "Daily budget", kind: "money" },
    { key: "approved_merchants", label: "Approved merchants", kind: "list" },
    { key: "max_transaction_minor", label: "Max per transaction", kind: "moneyOpt" },
    { key: "duplicate_window_minutes", label: "Duplicate window (minutes)", kind: "int" },
    { key: "duplicate_effect", label: "Duplicate effect", kind: "enum", options: ["DENY", "REQUIRE_APPROVAL"] },
  ],
  user: [
    { key: "daily_limit_minor", label: "Daily limit", kind: "money" },
    { key: "approval_threshold_minor", label: "Approval threshold", kind: "money", help: "Above this a human must approve." },
    { key: "max_transaction_minor", label: "Max per transaction", kind: "moneyOpt" },
  ],
  merchant: [
    { key: "max_transaction_minor", label: "Max per transaction", kind: "moneyOpt" },
    { key: "allowed_categories", label: "Allowed categories", kind: "listOpt", help: "Empty = all categories." },
    { key: "allowed_agents", label: "Allowed agents", kind: "listOpt", help: "Empty = all agents." },
    { key: "approval_required_above_minor", label: "Approval required above", kind: "moneyOpt" },
  ],
  agent: [
    { key: "enabled", label: "Enabled", kind: "bool" },
    { key: "max_transaction_minor", label: "Max per transaction", kind: "money" },
    { key: "daily_limit_minor", label: "Daily limit", kind: "money" },
    { key: "allowed_merchants", label: "Allowed merchants", kind: "listOpt", help: "Empty = all merchants." },
    { key: "approval_required_above_minor", label: "Approval required above", kind: "moneyOpt" },
  ],
};

const SCOPE_ORDER: Policy["scope"][] = ["system", "org", "user", "merchant", "agent"];
type Draft = Record<string, string | boolean>;

function toDraft(scope: Policy["scope"], rules: Record<string, unknown>): Draft {
  const d: Draft = {};
  for (const f of SCHEMA[scope]) {
    const v = rules[f.key];
    if (f.kind === "bool") d[f.key] = v === true;
    else if (f.kind === "list" || f.kind === "listOpt") d[f.key] = Array.isArray(v) ? v.join(", ") : "";
    else d[f.key] = v == null ? "" : String(v);
  }
  return d;
}

/** Build the rules document; returns a client-side error per field only for unparsable input. */
function toRules(scope: Policy["scope"], d: Draft): { rules: Record<string, unknown>; errors: Record<string, string> } {
  const rules: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of SCHEMA[scope]) {
    const raw = d[f.key];
    if (f.kind === "bool") rules[f.key] = raw === true;
    else if (f.kind === "list" || f.kind === "listOpt") {
      const items = String(raw).split(",").map((x) => x.trim()).filter(Boolean);
      rules[f.key] = f.kind === "listOpt" && items.length === 0 ? null : items;
    } else if (f.kind === "enum") rules[f.key] = String(raw);
    else {
      const s = String(raw).trim();
      if (s === "" && f.kind === "moneyOpt") rules[f.key] = null;
      else if (!/^\d+$/.test(s)) errors[f.key] = "Whole number of minor units (e.g. cents)";
      else rules[f.key] = Number(s);
    }
  }
  return { rules, errors };
}

/** Pydantic error loc like ["max_transaction_minor"] -> field key. */
function serverFieldErrors(e: unknown): { byField: Record<string, string>; general: string[] } {
  const byField: Record<string, string> = {};
  const general: string[] = [];
  if (e instanceof ApiError && Array.isArray(e.details.errors)) {
    for (const it of e.details.errors as unknown[]) {
      if (it && typeof it === "object" && "msg" in it) {
        const loc = (it as { loc?: unknown[] }).loc ?? [];
        const msg = String((it as { msg: unknown }).msg);
        if (loc.length > 0) byField[String(loc[0])] = msg;
        else general.push(msg);
      } else general.push(String(it));
    }
  }
  return { byField, general };
}

function PolicyForm({ p, currency, onSaved, onConflict }: { p: Policy; currency: string; onSaved: () => void; onConflict: () => void }) {
  const [draft, setDraft] = useState<Draft>(() => toDraft(p.scope, p.rules));
  const [clientErr, setClientErr] = useState<Record<string, string>>({});
  const [serverErr, setServerErr] = useState<{ byField: Record<string, string>; general: string[] }>({ byField: {}, general: [] });
  const [err, setErr] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setDraft(toDraft(p.scope, p.rules)); // server version moved on: show what is stored
  }, [p.version, p.scope, p.rules]);

  const dirty = JSON.stringify(draft) !== JSON.stringify(toDraft(p.scope, p.rules));
  const conflict = err instanceof ApiError && err.status === 409;

  const save = async () => {
    const { rules, errors } = toRules(p.scope, draft);
    setClientErr(errors);
    setSaved(false);
    if (Object.keys(errors).length) return;
    setBusy(true);
    setErr(null);
    setServerErr({ byField: {}, general: [] });
    try {
      await api.updatePolicy(p.id, rules, p.version);
      setSaved(true);
      onSaved();
    } catch (e) {
      setErr(e as Error);
      setServerErr(serverFieldErrors(e));
      if (e instanceof ApiError && e.status === 409) onConflict();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-md border border-slate-200">
      <button className="flex w-full items-center justify-between px-3 py-2 text-left hover:bg-slate-50" onClick={() => setOpen(!open)}>
        <span>
          <span className="font-mono text-sm font-medium">{p.id}</span>
          <span className="ml-2 text-xs text-slate-500">v{p.version}</span>
        </span>
        <span className="text-xs text-slate-400">
          {p.updated_by ? `${p.updated_by} · ` : ""}
          {fmtTime(p.updated_at)} {open ? "▲" : "▼"}
        </span>
      </button>
      {open && (
        <div className="space-y-3 border-t border-slate-100 p-3">
          <div className="grid gap-3 md:grid-cols-2">
            {SCHEMA[p.scope].map((f) => {
              const v = draft[f.key];
              const e = clientErr[f.key] ?? serverErr.byField[f.key];
              const set = (x: string | boolean) => setDraft({ ...draft, [f.key]: x });
              return (
                <label key={f.key} className="block text-xs text-slate-600">
                  {f.label}
                  {f.kind === "bool" ? (
                    <input type="checkbox" className="ml-2 align-middle" checked={v === true} onChange={(ev) => set(ev.target.checked)} />
                  ) : f.kind === "enum" && f.options && f.options.length > 0 ? (
                    <select className={`${inputCls} mt-1`} value={String(v)} onChange={(ev) => set(ev.target.value)}>
                      {f.options.map((o) => (
                        <option key={o}>{o}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      className={`${inputCls} mt-1 ${e ? "border-red-400" : ""}`}
                      value={String(v)}
                      inputMode={f.kind === "money" || f.kind === "moneyOpt" || f.kind === "int" ? "numeric" : "text"}
                      placeholder={f.kind === "moneyOpt" ? "none" : f.kind === "listOpt" ? "all (comma separated)" : ""}
                      onChange={(ev) => set(ev.target.value)}
                    />
                  )}
                  {(f.kind === "money" || f.kind === "moneyOpt") && /^\d+$/.test(String(v).trim()) && (
                    <span className="mt-0.5 block text-slate-500">= {formatMoney(Number(String(v).trim()), currency)} (minor units)</span>
                  )}
                  {f.help && <span className="mt-0.5 block text-slate-400">{f.help}</span>}
                  {e && <span className="mt-0.5 block text-red-700">{e}</span>}
                </label>
              );
            })}
          </div>
          {serverErr.general.map((m, i) => (
            <p key={i} className="text-sm text-red-700">
              {m}
            </p>
          ))}
          {err && !conflict && Object.keys(serverErr.byField).length === 0 && serverErr.general.length === 0 && <ErrorNote error={err} />}
          {err && !conflict && (Object.keys(serverErr.byField).length > 0 || serverErr.general.length > 0) && (
            <p className="text-sm text-red-700">The server rejected this policy: {err.message}</p>
          )}
          {conflict && (
            <div role="alert" className="flex items-center gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
              Someone else changed this policy (it is no longer at v{p.version}). Your edits were not saved.
              <Button variant="ghost" onClick={() => { setErr(null); onConflict(); }}>
                Reload policies
              </Button>
            </div>
          )}
          {saved && <p className="text-sm text-emerald-700">Saved.</p>}
          <div className="flex gap-2">
            <Button disabled={busy || !dirty} onClick={save}>
              {busy ? "Saving…" : `Save (expects v${p.version})`}
            </Button>
            <Button variant="ghost" disabled={!dirty} onClick={() => { setDraft(toDraft(p.scope, p.rules)); setClientErr({}); setServerErr({ byField: {}, general: [] }); setErr(null); }}>
              Reset
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

export function Policies() {
  const { data, error, loading, refresh } = usePoll(api.policies, 0);
  const ov = usePoll(api.overview, 0);
  const currency = ov.data?.currency ?? "USD";
  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote error={error} />;
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <p className="text-sm text-slate-600">
        Limits are integer minor units (cents). Policies are validated by the server; saving bumps the version and is audited.
      </p>
      {SCOPE_ORDER.map((scope) => {
        const items = data.policies.filter((p) => p.scope === scope);
        if (items.length === 0) return null;
        return (
          <Card key={scope} title={`${scope[0].toUpperCase()}${scope.slice(1)} policies`}>
            <div className="space-y-2">
              {items.map((p) => (
                <PolicyForm key={p.id} p={p} currency={currency} onSaved={refresh} onConflict={refresh} />
              ))}
            </div>
          </Card>
        );
      })}
    </div>
  );
}
