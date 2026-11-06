"use client";

import { useState, type FormEvent } from "react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { useMe } from "@/components/Shell";
import { Badge, Banner, Button, Card, Empty, ErrorBox, Field, inputClass, Loading, TableWrap, Td, Th } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { ago, dateTime, daysLeft, signedUsd, tone, usd } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { AdminOverview, AuditRow, Me, PaymentRow } from "@/lib/types";

type Pending = null | { kind: "pause" | "flatten" | "resume" | "user-pause" | "user-resume"; user?: string };

function KillSwitch({
  data,
  reload,
  pending,
  setPending,
  reason,
  setReason,
}: {
  data: AdminOverview;
  reload: () => void;
  pending: Pending;
  setPending: (p: Pending) => void;
  reason: string;
  setReason: (r: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const globalSwitch = data.kill_switch.find((k) => k.scope === "global");

  async function run() {
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      if (pending.kind === "resume" || pending.kind === "user-resume") {
        await api("/api/admin/resume", { method: "POST", body: { user_id: pending.user ?? null } });
      } else {
        await api("/api/admin/kill", {
          method: "POST",
          body: { user_id: pending.user ?? null, flatten: pending.kind === "flatten", reason },
        });
      }
      setPending(null);
      reload();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Kill switch" right={<Badge tone={globalSwitch ? "down" : "up"}>{globalSwitch ? (globalSwitch.flatten ? "flatten active" : "paused") : "trading on"}</Badge>}>
      {data.kill_switch.length > 0 && (
        <ul className="mb-4 space-y-1 font-mono text-xs">
          {data.kill_switch.map((k) => (
            <li key={k.scope} className="text-warn">
              {k.scope === "global" ? "ALL ACCOUNTS" : k.scope}: {k.flatten ? "closing everything" : "paused"}
              {k.reason ? ` (${k.reason})` : ""} · set by {k.set_by ?? "unknown"}
            </li>
          ))}
        </ul>
      )}
      <Field label="Reason (shown in the logs and to users)">
        <input className={inputClass} value={reason} maxLength={200} onChange={(e) => setReason(e.target.value)} placeholder="e.g. exchange outage, news event" />
      </Field>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="default" onClick={() => setPending({ kind: "pause" })}>Pause all new trades</Button>
        <Button variant="danger" onClick={() => setPending({ kind: "flatten" })}>Close everything now</Button>
        <Button variant="primary" onClick={() => setPending({ kind: "resume" })} disabled={!globalSwitch}>Resume all</Button>
      </div>
      <p className="mt-3 text-xs text-mute">
        Takes effect within about a minute, and only while the bot is running. Pausing keeps stop-loss orders. &ldquo;Close everything&rdquo; also cancels stops and closes positions at market.
      </p>
      {error && <div className="mt-3"><ErrorBox message={error} /></div>}
      <ConfirmModal
        open={pending?.kind === "flatten"}
        danger
        title="Close every position?"
        confirmWord="FLATTEN"
        confirmLabel="Close everything"
        busy={busy}
        onConfirm={run}
        onCancel={() => setPending(null)}
      >
        <p>Every open position for {pending?.user ?? "all accounts"} is closed at market price and its stop-loss is cancelled. Trading stays off until you resume.</p>
      </ConfirmModal>
      <ConfirmModal
        open={pending?.kind === "pause" || pending?.kind === "user-pause"}
        title="Pause new trades?"
        confirmLabel="Pause"
        busy={busy}
        onConfirm={run}
        onCancel={() => setPending(null)}
      >
        <p>No new trades open for {pending?.user ?? "all accounts"}. Open positions keep their stops.</p>
      </ConfirmModal>
      <ConfirmModal
        open={pending?.kind === "resume" || pending?.kind === "user-resume"}
        title="Resume trading?"
        confirmLabel="Resume"
        busy={busy}
        onConfirm={run}
        onCancel={() => setPending(null)}
      >
        <p>Trading continues for {pending?.user ?? "all accounts"} on the next check.</p>
      </ConfirmModal>
    </Card>
  );
}

function EditUser({ user, onDone }: { user: Me; onDone: () => void }) {
  const [cap, setCap] = useState(String(user.max_wallet_cap_usdt));
  const [lev, setLev] = useState(user.leverage);
  const [mode, setMode] = useState(user.trading_mode);
  const [confirmReal, setConfirmReal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const goingLive = mode === "live" && user.trading_mode !== "live";

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(`/api/admin/users/${encodeURIComponent(user.user_id)}`, {
        method: "PATCH",
        body: { max_wallet_cap_usdt: Number(cap), leverage: lev, trading_mode: mode, confirm_real_money: confirmReal },
      });
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={`Edit ${user.user_id}`}>
      <form onSubmit={save} className="grid gap-4 sm:grid-cols-3">
        <Field label="Wallet cap (USDT)" hint="The most of this account the bot may use. 0 blocks live trading.">
          <input className={inputClass} type="number" min={0} step="any" value={cap} onChange={(e) => setCap(e.target.value)} />
        </Field>
        <Field label="Leverage">
          <select className={inputClass} value={lev} onChange={(e) => setLev(Number(e.target.value))}>
            {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}x</option>)}
          </select>
        </Field>
        <Field label="Mode">
          <select className={inputClass} value={mode} onChange={(e) => setMode(e.target.value as Me["trading_mode"])}>
            <option value="testnet">Paper</option>
            <option value="live">Live (real money)</option>
          </select>
        </Field>
        {goingLive && (
          <label className="flex items-start gap-2 text-sm sm:col-span-3">
            <input type="checkbox" checked={confirmReal} onChange={(e) => setConfirmReal(e.target.checked)} className="mt-1" />
            <span>I understand this account will trade <b className="text-warn">real funds</b>. Its API key has no withdrawal permission and its Binance account is in One-way mode.</span>
          </label>
        )}
        {error && <div className="sm:col-span-3"><ErrorBox message={error} /></div>}
        <div className="flex gap-2 sm:col-span-3">
          <Button type="submit" variant="primary" disabled={busy || (goingLive && !confirmReal)}>Save</Button>
          <Button type="button" variant="ghost" onClick={onDone}>Cancel</Button>
        </div>
      </form>
    </Card>
  );
}

function CreateUser({ onDone }: { onDone: () => void }) {
  const [id, setId] = useState("");
  const [days, setDays] = useState("30");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function create(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/admin/users", { method: "POST", body: { user_id: id.trim().toLowerCase(), subscription_days: Number(days) || 0 } });
      setId("");
      onDone();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Add a user">
      <form onSubmit={create} className="grid gap-4 sm:grid-cols-[2fr_1fr_auto] sm:items-end">
        <Field label="Login email" hint="Must match the email they sign in with. Invite them in Supabase first. New profiles start in paper mode.">
          <input className={inputClass} type="email" required value={id} onChange={(e) => setId(e.target.value)} />
        </Field>
        <Field label="Subscription days">
          <input className={inputClass} type="number" min={0} value={days} onChange={(e) => setDays(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" disabled={busy || !id}>Add</Button>
        {error && <div className="sm:col-span-3"><ErrorBox message={error} /></div>}
      </form>
    </Card>
  );
}

function Users({ data, reload, setPending }: { data: AdminOverview; reload: () => void; setPending: (p: Pending) => void }) {
  const [editing, setEditing] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function extend(u: Me) {
    setError(null);
    try {
      await api(`/api/admin/users/${encodeURIComponent(u.user_id)}/extend`, { method: "POST", body: { days: 30 } });
      reload();
    } catch (e) {
      setError(errorText(e));
    }
  }

  function switchUser(u: Me) {
    setPending({ kind: u.control.paused ? "user-resume" : "user-pause", user: u.user_id });
  }

  return (
    <>
      <Card title={`Users (${data.users.length} of 11)`}>
        {error && <div className="mb-3"><ErrorBox message={error} /></div>}
        <TableWrap>
          <thead>
            <tr><Th>User</Th><Th>Mode</Th><Th right>Cap</Th><Th right>Lev</Th><Th>Keys</Th><Th>Subscription</Th><Th right>Today</Th><Th>State</Th><Th /></tr>
          </thead>
          <tbody className="divide-y divide-line">
            {data.users.map((u) => {
              const left = daysLeft(u.subscription.expires_at);
              return (
                <tr key={u.user_id}>
                  <Td>{u.user_id}{u.is_admin && <span className="ml-2"><Badge tone="accent">admin</Badge></span>}</Td>
                  <Td><Badge tone={u.trading_mode === "live" ? "warn" : "accent"}>{u.trading_mode === "live" ? "live" : "paper"}</Badge></Td>
                  <Td right>{usd(u.max_wallet_cap_usdt, 0)}</Td>
                  <Td right>{u.leverage}x</Td>
                  <Td tone={u.keys_stored ? "up" : "mute"}>{u.keys_stored ? "stored" : "none"}</Td>
                  <Td tone={u.is_admin || u.subscription.active ? "up" : "warn"}>{u.is_admin ? "n/a" : u.subscription.active ? `${left}d left` : "expired"}</Td>
                  <Td right tone={tone(u.today.pnl)}>{signedUsd(u.today.pnl)}</Td>
                  <Td>
                    {u.control.flatten ? <Badge tone="down">flatten</Badge> : u.control.paused ? <Badge tone="warn">paused</Badge> : u.today.breaker_tripped ? <Badge tone="warn">loss limit</Badge> : <Badge tone="up">on</Badge>}
                  </Td>
                  <Td>
                    <span className="flex gap-1">
                      <Button variant="ghost" onClick={() => setEditing(u)}>Edit</Button>
                      {!u.is_admin && <Button variant="ghost" onClick={() => extend(u)}>+30d</Button>}
                      {u.control.scope !== "global" && <Button variant="ghost" onClick={() => switchUser(u)}>{u.control.paused ? "Resume" : "Pause"}</Button>}
                    </span>
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </TableWrap>
      </Card>
      {editing && <EditUser user={editing} onDone={() => { setEditing(null); reload(); }} />}
      {data.users.length < 11 && <CreateUser onDone={reload} />}
    </>
  );
}

function Activity() {
  const audit = useApi<AuditRow[]>("/api/admin/audit?limit=30", 60_000);
  const pays = useApi<PaymentRow[]>("/api/admin/payments?limit=20", 60_000);
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card title="Recent admin actions">
        {audit.loading ? <Loading /> : audit.error ? <ErrorBox message={audit.error} /> : !audit.data?.length ? <Empty>Nothing yet.</Empty> : (
          <ul className="space-y-1.5 font-mono text-xs">
            {audit.data.map((a, i) => (
              <li key={i} className="flex min-w-0 gap-2 overflow-hidden">
                <span className="shrink-0 text-mute">{dateTime(a.at).slice(5, 16)}</span>
                <span className="shrink-0 text-ink">{a.actor}</span>
                <span className="shrink-0 text-accent">{a.action}</span>
                <span className="min-w-0 truncate text-mute">{[a.target, a.detail].filter(Boolean).join(" · ")}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card title="Payments">
        {pays.loading ? <Loading /> : pays.error ? <ErrorBox message={pays.error} /> : !pays.data?.length ? <Empty>No payments.</Empty> : (
          <ul className="space-y-1.5 font-mono text-xs">
            {pays.data.map((p) => (
              <li key={p.order_id} className="flex flex-wrap gap-x-2">
                <span className="text-mute">{dateTime(p.created_at).slice(5, 16)}</span>
                <span className="text-ink">{p.user_id}</span>
                <span>{usd(p.price_usd)}</span>
                <Badge tone={p.credited ? "up" : "mute"}>{p.credited ? "credited" : p.status.replace("_", " ")}</Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

export default function AdminPage() {
  const { me } = useMe();
  const { data, error, loading, reload } = useApi<AdminOverview>(me.is_admin ? "/api/admin/overview" : null, 15_000);
  const [pending, setPending] = useState<Pending>(null);
  const [reason, setReason] = useState("");

  if (!me.is_admin) return <Banner tone="warn" title="Administrator access required" />;
  if (loading) return <Loading />;
  if (error || !data) return <ErrorBox message={error ?? "Could not load the overview."} />;

  const r = data.runner;
  return (
    <>
      {!r.alive && (
        <Banner tone="down" title="The bot is not reporting">
          {r.seconds_since === null ? "It has never checked in." : `Last check-in was ${ago(new Date(Date.now() - r.seconds_since * 1000).toISOString())}.`} The kill switch only works while the bot is running. Check the service on the server.
        </Banner>
      )}
      <div className="grid gap-4 lg:grid-cols-[1fr_2fr]">
        <Card title="Bot status">
          <div className="space-y-1 font-mono text-sm">
            <div className="flex items-center gap-2"><span className={`h-2.5 w-2.5 rounded-full ${r.alive ? "bg-up" : "bg-down"}`} />{r.alive ? "running" : "not reporting"}</div>
            <div className="text-mute">{r.info ?? "no information"}</div>
            <div className="text-mute">open managed trades: <span className="text-ink">{data.open_trades}</span></div>
          </div>
        </Card>
        <KillSwitch data={data} reload={reload} pending={pending} setPending={setPending} reason={reason} setReason={setReason} />
      </div>
      <Users data={data} reload={reload} setPending={setPending} />
      <Activity />
    </>
  );
}
