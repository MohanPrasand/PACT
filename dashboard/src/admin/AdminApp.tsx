import { useEffect, useState } from "react";
import { adminToken } from "../lib/adminToken";
import { matchRoute } from "../lib/useHashRoute";
import { Shell } from "../shared/Shell";
import { Approvals } from "./Approvals";
import { Audit } from "./Audit";
import { Login } from "./Login";
import { Overview } from "./Overview";
import { Policies } from "./Policies";
import { RequestPage } from "./RequestPage";

const SCREENS = [
  { path: "/admin", label: "Overview" },
  { path: "/admin/approvals", label: "Approvals" },
  { path: "/admin/policies", label: "Policies" },
  { path: "/admin/audit", label: "Audit" },
];

export function AdminApp({ route }: { route: string }) {
  const [authed, setAuthed] = useState(() => adminToken.get() !== null);
  useEffect(() => {
    const out = () => setAuthed(false);
    window.addEventListener("pact:admin-logout", out);
    return () => window.removeEventListener("pact:admin-logout", out);
  }, []);

  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  const path = route.split("?")[0];
  const reqMatch = matchRoute("/admin/requests/:id", path);
  const screen = SCREENS.find((s) => matchRoute(s.path, path)) ?? SCREENS[0];
  const body = reqMatch ? (
    <RequestPage id={reqMatch.id} />
  ) : screen.path === "/admin/approvals" ? (
    <Approvals route={route} />
  ) : screen.path === "/admin/policies" ? (
    <Policies />
  ) : screen.path === "/admin/audit" ? (
    <Audit />
  ) : (
    <Overview />
  );
  return (
    <Shell
      tone="admin"
      brand="PACT"
      tagline="Policy and Authorization Control Tower"
      route={route}
      nav={SCREENS.map((s) => ({ href: `#${s.path}`, label: s.label, match: s.path === "/admin" ? "/admin/__overview" : s.path }))}
      right={
        <button
          className="rounded px-3 py-1.5 text-sm hover:bg-white/10"
          onClick={() => {
            adminToken.clear();
            setAuthed(false);
          }}
        >
          Sign out
        </button>
      }
    >
      {body}
    </Shell>
  );
}
