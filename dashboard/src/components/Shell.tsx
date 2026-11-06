"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, errorText } from "@/lib/api";
import { ago } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { configured, supabase } from "@/lib/supabase";
import type { Health, Me } from "@/lib/types";
import { Badge, Button, Card, ErrorBox, Loading } from "./ui";

interface MeContextValue {
  me: Me;
  reload: () => void;
}

const MeContext = createContext<MeContextValue | null>(null);

export function useMe(): MeContextValue {
  const v = useContext(MeContext);
  if (!v) throw new Error("useMe must be used inside the Shell");
  return v;
}

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/trades", label: "Trades" },
  { href: "/settings", label: "Settings" },
  { href: "/billing", label: "Billing" },
];

function RunnerDot() {
  const { data } = useApi<Health>("/api/health", 30_000);
  if (!data) return null;
  const alive = data.runner.alive;
  return (
    <span className="flex items-center gap-1.5 font-mono text-[11px] text-mute" title={data.runner.info ?? ""}>
      <span className={`h-2 w-2 rounded-full ${alive ? "bg-up" : "bg-down"}`} aria-hidden />
      <span className="hidden sm:inline">
        bot {alive ? "running" : "not reporting"}
        {data.runner.seconds_since !== null && ` · ${ago(new Date(Date.now() - data.runner.seconds_since * 1000).toISOString())}`}
      </span>
      <span className="sm:hidden">{alive ? "bot ok" : "bot down"}</span>
    </span>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [session, setSession] = useState<"loading" | "in" | "out">("loading");
  const [me, setMe] = useState<Me | null>(null);
  const [problem, setProblem] = useState<{ status: number; text: string } | null>(null);
  const onLogin = pathname === "/login";

  useEffect(() => {
    if (!configured) return;
    const sb = supabase();
    sb.auth.getSession().then(({ data }) => setSession(data.session ? "in" : "out"));
    const { data } = sb.auth.onAuthStateChange((_event, s) => setSession(s ? "in" : "out"));
    return () => data.subscription.unsubscribe();
  }, []);

  const loadMe = useCallback(async () => {
    try {
      setMe(await api<Me>("/api/me"));
      setProblem(null);
    } catch (e) {
      setProblem({ status: e instanceof ApiError ? e.status : 0, text: errorText(e) });
    }
  }, []);

  useEffect(() => {
    if (session === "in") loadMe();
    if (session === "out") setMe(null);
  }, [session, loadMe]);

  useEffect(() => {
    if (session === "out" && !onLogin) router.replace("/login");
    if (session === "in" && onLogin) router.replace("/");
  }, [session, onLogin, router]);

  if (!configured) {
    return (
      <main className="mx-auto max-w-lg p-6">
        <Card title="Setup needed">
          <p className="text-sm">
            The dashboard is missing <code className="font-mono text-accent">NEXT_PUBLIC_SUPABASE_URL</code>,{" "}
            <code className="font-mono text-accent">NEXT_PUBLIC_SUPABASE_ANON_KEY</code> and{" "}
            <code className="font-mono text-accent">NEXT_PUBLIC_API_URL</code>. Set them in the Vercel project settings and redeploy.
          </p>
        </Card>
      </main>
    );
  }

  if (onLogin) return <>{children}</>;
  if (session === "loading") return <main className="p-6"><Loading /></main>;
  if (session === "out") return null;

  if (problem && !me) {
    const noProfile = problem.status === 403;
    return (
      <main className="mx-auto max-w-lg space-y-4 p-6">
        <Card title={noProfile ? "No profile for this login" : "Cannot load your account"}>
          <p className="text-sm text-ink/90">{problem.text}</p>
          <div className="mt-4 flex gap-2">
            {!noProfile && <Button onClick={loadMe}>Try again</Button>}
            <Button variant="ghost" onClick={() => supabase().auth.signOut()}>Sign out</Button>
          </div>
        </Card>
      </main>
    );
  }
  if (!me) return <main className="p-6"><Loading /></main>;

  const items = me.is_admin ? [...NAV, { href: "/admin", label: "Admin" }] : NAV;
  return (
    <MeContext.Provider value={{ me, reload: loadMe }}>
      <div className="min-h-screen">
        <header className="sticky top-0 z-30 border-b border-line bg-bg/95 backdrop-blur">
          <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5">
            <Link href="/" className="font-mono text-sm font-bold tracking-[0.3em] text-accent">G-AEGISBOT</Link>
            <nav className="order-last -mx-1 flex w-full gap-1 overflow-x-auto md:order-none md:w-auto md:flex-1" aria-label="Main">
              {items.map((i) => {
                const active = i.href === "/" ? pathname === "/" : pathname.startsWith(i.href);
                return (
                  <Link
                    key={i.href}
                    href={i.href}
                    aria-current={active ? "page" : undefined}
                    className={`whitespace-nowrap rounded px-2.5 py-1 font-mono text-xs uppercase tracking-wider ${
                      active ? "bg-raised text-ink" : "text-mute hover:text-ink"
                    }`}
                  >
                    {i.label}
                  </Link>
                );
              })}
            </nav>
            <div className="ml-auto hidden items-center gap-3 md:flex">
              <RunnerDot />
              <Badge tone={me.trading_mode === "live" ? "warn" : "accent"}>{me.trading_mode === "live" ? "live" : "paper"}</Badge>
            </div>
            <Button variant="ghost" className="ml-auto md:ml-0" onClick={() => supabase().auth.signOut()} title={me.user_id}>
              Sign out
            </Button>
          </div>
        </header>
        <main className="mx-auto max-w-6xl space-y-4 px-4 py-5">{problem && <ErrorBox message={problem.text} />}{children}</main>
      </div>
    </MeContext.Provider>
  );
}
