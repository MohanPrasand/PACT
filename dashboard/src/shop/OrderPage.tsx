import { useEffect, useRef, useState, type ReactNode } from "react";
import { usePoll } from "../lib/usePoll";
import { formatMoney } from "../lib/money";
import { StatusBadge } from "../shared/StatusBadge";
import { Button, Card, ErrorNote, Spinner } from "../shared/ui";
import { api, isAgentRef } from "./api";
import type { OrderStatus, WebCheckoutResult } from "./types";

type StepState = "done" | "active" | "failed" | "skipped" | "todo";
interface Step { title: string; state: StepState; detail: ReactNode }

const WAITING = new Set(["PENDING_APPROVAL", "APPROVED"]);
const PAID_TERMINAL = new Set(["CAPTURED", "CANCELLED"]);

export function isTerminal(s: OrderStatus): boolean {
  if (PAID_TERMINAL.has(s.payment_status)) return true;
  const a = s.authorization;
  return !!a && a.terminal;
}

function buildSteps(s: OrderStatus): Step[] {
  const { order, authorization: a, payment_status: pay } = s;
  const steps: Step[] = [{ title: "Order created", state: "done", detail: <>{order.merchant_name ?? order.merchant_id}, {formatMoney(order.total_minor, order.currency)}</> }];

  // 2 authorization + decision
  if (!a) {
    steps.push({ title: "Authorization requested", state: "todo", detail: "Not requested yet." });
  } else if (!a.decision) {
    steps.push({ title: "Authorization requested", state: "active", detail: "Evaluating policy…" });
  } else {
    steps.push({
      title: "Authorization and policy decision",
      state: a.decision === "DENY" ? "failed" : "done",
      detail: <><StatusBadge status={a.decision} />{a.reason_codes.length > 0 && <span className="ml-2 font-mono text-xs">{a.reason_codes.join(", ")}</span>}</>,
    });
  }

  // 3 human approval
  const denied = a?.decision === "DENY";
  if (!a || !a.decision) steps.push({ title: "Human approval", state: "todo", detail: "Pending the policy decision." });
  else if (denied) steps.push({ title: "Human approval", state: "skipped", detail: "Not reached: denied by policy." });
  else if (a.decision === "ALLOW") steps.push({ title: "Human approval", state: "skipped", detail: "Not required for this purchase." });
  else if (a.state === "PENDING_APPROVAL") steps.push({ title: "Human approval", state: "active", detail: "Pending. A human must approve in the PACT dashboard or with `pact approve`." });
  else if (a.state === "DENIED") steps.push({ title: "Human approval", state: "failed", detail: `Denied${a.decided_by ? ` by ${a.decided_by}` : ""}.` });
  else if (a.state === "EXPIRED") steps.push({ title: "Human approval", state: "failed", detail: "The approval request expired." });
  else steps.push({ title: "Human approval", state: "done", detail: `Approved${a.decided_by ? ` by ${a.decided_by}` : ""}.` });

  // 4 grant
  if (a?.grant?.issued) steps.push({ title: "Grant issued and consumed", state: a.grant.consumed ? "done" : "active", detail: a.grant.consumed ? "Single-use grant issued and consumed." : "Grant issued; waiting to be used." });
  else if (a && (denied || a.state === "DENIED" || a.state === "EXPIRED" || a.state === "REVOKED")) steps.push({ title: "Grant issued and consumed", state: "skipped", detail: "No grant was issued." });
  else steps.push({ title: "Grant issued and consumed", state: "todo", detail: "A single-use grant is issued once the request is approved." });

  // 5 payment
  const label = a?.provider_label ?? "payment provider";
  let pstep: Step;
  if (pay === "CAPTURED") pstep = { title: "Payment captured", state: "done", detail: <>{label}{a?.capture_id && <> · capture <span className="font-mono text-xs">{a.capture_id}</span></>}</> };
  else if (pay === "FAILED") pstep = { title: "Payment", state: "failed", detail: <>Failed{a?.failure_reason ? `: ${a.failure_reason}` : "."}</> };
  else if (pay === "CANCELLED") pstep = { title: "Payment", state: "failed", detail: "Cancelled." };
  else if (a?.state === "EXECUTING" || pay === "PENDING" || pay === "APPROVED") pstep = { title: "Payment", state: "active", detail: <>{label} checkout pending{a?.provider_order_id && <> · <span className="font-mono text-xs">{a.provider_order_id}</span></>}</> };
  else pstep = { title: "Payment", state: "todo", detail: "Starts after the grant is issued." };
  steps.push(pstep);
  return steps;
}

const DOT: Record<StepState, string> = {
  done: "bg-emerald-500 text-white", active: "bg-amber-400 text-white animate-pulse", failed: "bg-red-500 text-white",
  skipped: "bg-slate-200 text-slate-500", todo: "bg-white text-slate-400 ring-1 ring-slate-300",
};
const GLYPH: Record<StepState, string> = { done: "✓", active: "…", failed: "✕", skipped: "–", todo: "" };

export function OrderPage({ id, start }: { id: string; start: boolean }) {
  const [checkout, setCheckout] = useState<WebCheckoutResult | null>(null);
  const [err, setErr] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  const started = useRef(false);

  const poll = usePoll(
    async () => {
      let s = await api.orderStatus(id);
      // Web orders: keep resuming the checkout while it waits on the human, so the grant is issued and the
      // payment executes as soon as the dashboard approves. Never approves anything itself.
      if (!isAgentRef(s.order.buyer_ref) && s.authorization && WAITING.has(s.authorization.state)) {
        try {
          setCheckout(await api.orderCheckout(id));
          s = await api.orderStatus(id);
        } catch (e) {
          setErr(e as Error);
        }
      }
      return s;
    },
    2000, isTerminal, [id],
  );
  const { data, error, loading, refresh } = poll;
  const s = data && data.order.order_id === id ? data : null;
  const web = !!s && !isAgentRef(s.order.buyer_ref);

  async function pay(retry = false) {
    setBusy(true);
    setErr(null);
    try {
      setCheckout(await api.orderCheckout(id, retry));
      refresh();
    } catch (e) {
      setErr(e as Error);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (start && !started.current && s && web && !s.authorization && s.payment_status === "CREATED") {
      started.current = true;
      void pay();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start, s, web]);

  const a = s?.authorization ?? null;
  const checkoutUrl = a?.checkout_url ?? checkout?.checkout_url ?? null;
  const showCheckout = !!checkoutUrl && s?.payment_status !== "CAPTURED" && s?.payment_status !== "CANCELLED" && (a?.state === "EXECUTING" || !a);
  const canRetry = web && !!s && (s.payment_status === "FAILED" || s.payment_status === "CANCELLED" || a?.state === "DENIED" || a?.state === "FAILED" || a?.state === "EXPIRED") && s.payment_status !== "CAPTURED";
  const waiting = !!a && WAITING.has(a.state) && a.decision === "REQUIRE_APPROVAL";

  return (
    <div className="space-y-4">
      <a href="#/shop/orders" className="text-sm text-teal-700 hover:underline">← Orders</a>
      <ErrorNote error={error} />
      <ErrorNote error={err} />
      {loading && !s && <Spinner />}
      {s && (
        <>
          <Card
            title={<span className="font-mono">{s.order.order_id}</span>}
            actions={<div className="flex items-center gap-2">
              {a?.provider_label && <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">{a.provider_label}</span>}
              <StatusBadge status={s.payment_status.toLowerCase()} label={s.payment_status} /></div>}
          >
            <div className="mb-3 text-xs text-slate-500">{s.order.merchant_name ?? s.order.merchant_id} · buyer {s.order.buyer_ref || "-"} {!isTerminal(s) && <span className="ml-2 text-emerald-600">live</span>}</div>
            <table className="w-full text-sm">
              <tbody>
                {s.order.items.map((i) => (
                  <tr key={i.product_id} className="border-t border-slate-100">
                    <td className="py-1.5">{i.name}</td><td className="py-1.5 text-slate-500">{i.quantity} × {formatMoney(i.unit_price_minor, s.order.currency)}</td>
                    <td className="py-1.5 text-right">{formatMoney(i.unit_price_minor * i.quantity, s.order.currency)}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot><tr className="border-t border-slate-200"><td colSpan={2} className="pt-2 text-right text-slate-500">Total</td><td className="pt-2 text-right font-semibold">{formatMoney(s.order.total_minor, s.order.currency)}</td></tr></tfoot>
            </table>
          </Card>

          {showCheckout && (
            <div className="rounded-lg border border-teal-300 bg-teal-50 p-4">
              <div className="mb-2 text-sm text-teal-900">Approved by PACT. Finish paying on {a?.provider_label ?? "the payment provider"}.</div>
              <a href={checkoutUrl!} className="inline-block rounded-md bg-teal-700 px-4 py-2 text-sm font-medium text-white hover:bg-teal-600">Continue to payment</a>
            </div>
          )}
          {waiting && web && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">
              Waiting for approval in the PACT dashboard (<code className="rounded bg-amber-100 px-1">#/admin</code>) or with <code className="rounded bg-amber-100 px-1">pact approve</code>.
              This page continues automatically once it is approved. The storefront cannot approve.
            </div>
          )}
          {waiting && !web && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900">Waiting for a human to approve this agent purchase in the PACT dashboard or with <code>pact approve</code>.</div>
          )}
          {a?.decision === "DENY" && (
            <div className="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-900">PACT denied this purchase. Reason codes: <span className="font-mono">{a.reason_codes.join(", ") || "none given"}</span></div>
          )}
          {web && !a && s.payment_status === "CREATED" && (
            <div className="flex items-center gap-3"><Button onClick={() => pay()} disabled={busy}>{busy ? "Starting…" : "Pay with PACT"}</Button><span className="text-xs text-slate-500">Starts a PACT-governed payment.</span></div>
          )}
          {canRetry && <Button variant="ghost" onClick={() => pay(true)} disabled={busy}>Try payment again</Button>}

          <Card title="Progress">
            <ol className="space-y-5">
              {buildSteps(s).map((st, i) => (
                <li key={i} className="flex gap-3">
                  <span className={`mt-0.5 flex h-8 w-8 flex-none items-center justify-center rounded-full text-sm font-bold ${DOT[st.state]}`}>{GLYPH[st.state] || i + 1}</span>
                  <div>
                    <div className={`text-base font-semibold ${st.state === "todo" || st.state === "skipped" ? "text-slate-400" : "text-slate-800"}`}>{st.title}</div>
                    <div className="text-base text-slate-600">{st.detail}</div>
                  </div>
                </li>
              ))}
            </ol>
          </Card>
        </>
      )}
    </div>
  );
}
