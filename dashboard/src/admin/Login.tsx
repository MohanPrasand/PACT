import { useState } from "react";
import { adminToken } from "../lib/adminToken";
import { adminApi } from "../lib/adminApi";
import { Button, ErrorNote } from "../shared/ui";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    const token = value.trim();
    if (!/^[\x21-\x7E]+$/.test(token)) {
      setError(new Error("The token must be plain visible ASCII characters with no spaces. Paste only the value after '='."));
      return;
    }
    setBusy(true);
    adminToken.set(token);
    try {
      await adminApi("/admin/overview"); // validates the token; a 403 clears it
      onLogin();
    } catch (err) {
      setError(err as Error);
    } finally {
      setBusy(false);
      setValue("");
    }
  }

  return (
    <div className="mx-auto mt-24 max-w-sm px-4">
      <form onSubmit={submit} className="space-y-4 rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <div>
          <h1 className="text-lg font-semibold">PACT admin</h1>
          <p className="text-sm text-slate-500">Enter the admin token (kept in this tab only).</p>
        </div>
        <input
          type="password"
          autoFocus
          autoComplete="off"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="Admin token"
          className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
        />
        <ErrorNote error={error} />
        <Button type="submit" disabled={busy || value.trim() === ""} className="w-full">
          {busy ? "Checking…" : "Sign in"}
        </Button>
        <a href="#/shop" className="block text-center text-xs text-slate-500 hover:underline">
          Go to the Merchant Lab storefront
        </a>
      </form>
    </div>
  );
}
