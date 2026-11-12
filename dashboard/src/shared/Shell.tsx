import type { ReactNode } from "react";

interface NavItem {
  href: string;
  label: string;
  match: string;
}

export function Shell({ brand, tagline, nav, route, tone, right, children }: {
  brand: string;
  tagline?: string;
  nav: NavItem[];
  route: string;
  tone: "admin" | "shop";
  right?: ReactNode;
  children: ReactNode;
}) {
  const bar = tone === "admin" ? "bg-slate-900 text-white" : "bg-teal-700 text-white";
  return (
    <div className="min-h-screen">
      <header className={bar}>
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
          <div>
            <div className="text-base font-semibold leading-tight">{brand}</div>
            {tagline && <div className="text-xs opacity-70">{tagline}</div>}
          </div>
          <nav className="flex flex-1 flex-wrap gap-1 text-sm">
            {nav.map((n) => {
              const active = route.startsWith(n.match);
              return (
                <a key={n.href} href={n.href} className={`rounded px-3 py-1.5 ${active ? "bg-white/20" : "hover:bg-white/10"}`}>
                  {n.label}
                </a>
              );
            })}
          </nav>
          {right}
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
    </div>
  );
}
