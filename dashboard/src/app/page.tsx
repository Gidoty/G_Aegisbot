"use client";

import { useState } from "react";
import { ConfirmModal } from "@/components/ConfirmModal";
import { EquityChart } from "@/components/EquityChart";
import { useMe } from "@/components/Shell";
import { Badge, Banner, Button, Card, Empty, ErrorBox, Loading, SideBadge, Stat, TableWrap, Td, Th } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { ago, dateTime, daysLeft, pct, price, shortSymbol, signedUsd, strategyLabel, tone, usd } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { EquityPoint, Health, Position } from "@/lib/types";

const RANGES = [
  { label: "24h", hours: 24 },
  { label: "7d", hours: 168 },
  { label: "30d", hours: 720 },
];
const DAILY_LIMIT = 0.04;

export default function OverviewPage() {
  const { me, reload } = useMe();
  const [hours, setHours] = useState(168);
  const equity = useApi<EquityPoint[]>(`/api/me/equity?hours=${hours}`, 60_000);
  const positions = useApi<Position[]>("/api/me/positions", 15_000);
  const health = useApi<Health>("/api/health", 30_000);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const points = equity.data ?? [];
  const latest = points.length ? points[points.length - 1].equity : null;
  const lossPct = me.today.loss_pct ?? 0;
  const gauge = Math.min(lossPct / DAILY_LIMIT, 1);
  const gaugeTone = gauge >= 1 ? "bg-down" : gauge >= 0.6 ? "bg-warn" : "bg-up";
  const left = daysLeft(me.subscription.expires_at);
  const paused = me.control.paused;

  async function togglePause() {
    setBusy(true);
    setActionError(null);
    try {
      await api(paused ? "/api/me/resume" : "/api/me/pause", { method: "POST" });
      reload();
    } catch (e) {
      setActionError(errorText(e));
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  }

  return (
    <>
      {me.control.flatten && (
        <Banner tone="down" title="Emergency exit in progress">
          The administrator ordered all positions closed{me.control.reason ? `: ${me.control.reason}` : ""}. No new trades will open until it is lifted.
        </Banner>
      )}
      {paused && !me.control.flatten && (
        <Banner tone="warn" title="Trading paused">
          {me.control.scope === "global" ? "The administrator paused all accounts" : "This account is paused"}
          {me.control.reason ? `: ${me.control.reason}` : ""}. Open positions keep their stop-loss orders.
        </Banner>
      )}
      {me.today.breaker_tripped && (
        <Banner tone="warn" title="Daily loss limit reached">
          The account is down {pct(me.today.loss_pct)} today, so no new trades open until 00:00 UTC. Open positions keep their stops. Trading resumes by itself.
        </Banner>
      )}
      {!me.subscription.active && !me.is_admin && (
        <Banner tone="warn" title="Subscription inactive">
          New trades are off. Open positions are still managed. Renew on the Billing page.
        </Banner>
      )}
      {health.data && !health.data.runner.alive && (
        <Banner tone="down" title="The bot is not reporting">
          The trading process has not checked in{health.data.runner.seconds_since !== null ? ` for ${Math.round(health.data.runner.seconds_since / 60)} minutes` : ""}. Positions keep their exchange stop-loss orders, but nothing new is managed. Tell the administrator.
        </Banner>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Card>
          <Stat label="Equity" value={usd(latest)} sub={latest === null ? "no samples yet" : `start of day ${usd(me.today.start_equity)}`} />
        </Card>
        <Card>
          <Stat label="Today" value={signedUsd(me.today.pnl)} tone={tone(me.today.pnl)} sub={me.today.date ?? "UTC day"} />
        </Card>
        <Card>
          <Stat label="Mode" value={me.trading_mode === "live" ? "LIVE" : "PAPER"} tone={me.trading_mode === "live" ? "warn" : "accent"} sub={`${me.leverage}x · cap ${usd(me.max_wallet_cap_usdt, 0)}`} />
        </Card>
        <Card>
          <Stat
            label="Subscription"
            value={me.is_admin ? "ADMIN" : left === null ? "none" : left > 0 ? `${left}d` : "expired"}
            tone={me.is_admin ? "accent" : left !== null && left > 3 ? "up" : "warn"}
            sub={!me.is_admin && me.subscription.expires_at ? `to ${dateTime(me.subscription.expires_at).slice(0, 10)}` : undefined}
          />
        </Card>
      </div>

      <Card title="Daily loss limit" right={<span className="font-mono text-xs text-mute">{pct(lossPct)} of {pct(DAILY_LIMIT, 0)}</span>}>
        <div className="h-2 overflow-hidden rounded bg-raised" role="progressbar" aria-valuenow={Math.round(gauge * 100)} aria-valuemin={0} aria-valuemax={100} aria-label="Share of the daily loss limit used">
          <div className={`h-full ${gaugeTone}`} style={{ width: `${Math.max(gauge * 100, lossPct > 0 ? 2 : 0)}%` }} />
        </div>
        <p className="mt-2 text-xs text-mute">If the account loses {pct(DAILY_LIMIT, 0)} in one UTC day, new trades stop until 00:00 UTC.</p>
      </Card>

      <Card
        title="Equity"
        right={
          <div className="flex gap-1">
            {RANGES.map((r) => (
              <button
                key={r.hours}
                onClick={() => setHours(r.hours)}
                aria-pressed={hours === r.hours}
                className={`rounded px-2 py-0.5 font-mono text-[11px] uppercase ${hours === r.hours ? "bg-raised text-ink" : "text-mute hover:text-ink"}`}
              >
                {r.label}
              </button>
            ))}
          </div>
        }
      >
        {equity.loading ? <Loading /> : equity.error ? <ErrorBox message={equity.error} /> : <EquityChart points={points} />}
      </Card>

      <Card title="Open positions" right={<Badge tone="mute">{positions.data?.length ?? 0} / 4</Badge>}>
        {positions.loading ? (
          <Loading />
        ) : positions.error ? (
          <ErrorBox message={positions.error} />
        ) : !positions.data?.length ? (
          <Empty>No open positions.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr>
                <Th>Pair</Th>
                <Th>Side</Th>
                <Th>Strategy</Th>
                <Th right>Entry</Th>
                <Th right>Stop</Th>
                <Th right>Best</Th>
                <Th right>Risk to stop</Th>
                <Th>Opened</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {positions.data.map((p) => {
                const risk = (p.side === "long" ? p.entry_price - p.stop_price : p.stop_price - p.entry_price) / p.entry_price;
                return (
                  <tr key={p.symbol}>
                    <Td>{shortSymbol(p.symbol)}</Td>
                    <Td><SideBadge side={p.side} /></Td>
                    <Td className="text-mute">{strategyLabel(p.strategy)}</Td>
                    <Td right>{price(p.entry_price)}</Td>
                    <Td right>{price(p.stop_price)}</Td>
                    <Td right>{price(p.best_price)}</Td>
                    <Td right tone={risk < 0 ? "up" : "mute"}>{risk < 0 ? "locked in " : ""}{pct(Math.abs(risk))}</Td>
                    <Td className="text-mute">{ago(p.opened_at)}</Td>
                  </tr>
                );
              })}
            </tbody>
          </TableWrap>
        )}
      </Card>

      <div className="flex flex-wrap items-center gap-3">
        <Button variant={paused ? "primary" : "default"} onClick={() => (paused ? togglePause() : setConfirm(true))} disabled={busy || me.control.scope === "global"}>
          {paused ? "Resume my trading" : "Pause my trading"}
        </Button>
        {me.control.scope === "global" && <span className="text-xs text-mute">Only the administrator can lift the global pause.</span>}
        {actionError && <ErrorBox message={actionError} />}
      </div>

      <ConfirmModal open={confirm} title="Pause your trading?" confirmLabel="Pause" busy={busy} onConfirm={togglePause} onCancel={() => setConfirm(false)}>
        <p>No new trades will open. Positions you already hold stay open and keep their stop-loss orders. You can resume at any time.</p>
      </ConfirmModal>
    </>
  );
}
