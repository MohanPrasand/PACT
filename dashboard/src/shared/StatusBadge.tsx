const TONES: Record<string, string> = {
  slate: "bg-slate-100 text-slate-700 ring-slate-200",
  amber: "bg-amber-100 text-amber-800 ring-amber-200",
  blue: "bg-blue-100 text-blue-800 ring-blue-200",
  green: "bg-emerald-100 text-emerald-800 ring-emerald-200",
  red: "bg-red-100 text-red-800 ring-red-200",
  gray: "bg-zinc-200 text-zinc-700 ring-zinc-300",
};

// Merchant payment statuses (spec 8.1) and PACT authorization states / decisions.
const STATUS_TONE: Record<string, keyof typeof TONES> = {
  created: "slate",
  pending: "amber",
  approved: "blue",
  captured: "green",
  failed: "red",
  cancelled: "gray",
  CREATED: "slate",
  EVALUATING: "slate",
  PENDING_APPROVAL: "amber",
  APPROVED: "blue",
  GRANTED: "blue",
  EXECUTING: "amber",
  EXECUTED: "green",
  UNKNOWN: "amber",
  DENIED: "red",
  FAILED: "red",
  EXPIRED: "gray",
  REVOKED: "gray",
  ALLOW: "green",
  REQUIRE_APPROVAL: "amber",
  DENY: "red",
};

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const tone = TONES[STATUS_TONE[status] ?? "slate"];
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
      {label ?? status.replace(/_/g, " ")}
    </span>
  );
}
