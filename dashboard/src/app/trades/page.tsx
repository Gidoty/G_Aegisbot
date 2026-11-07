"use client";

import { Badge, Card, Empty, ErrorBox, Loading, SideBadge, Stat, TableWrap, Td, Th } from "@/components/ui";
import { dateTime, price, shortSymbol, signedUsd, strategyLabel, tone, usd } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Trade } from "@/lib/types";

export default function TradesPage() {
  const { data, error, loading } = useApi<Trade[]>("/api/me/trades?limit=200", 60_000);
  const known = (data ?? []).filter((t) => t.pnl !== null);
  const net = known.reduce((sum, t) => sum + (t.pnl ?? 0), 0);
  const wins = known.filter((t) => (t.pnl ?? 0) > 0).length;

  return (
    <>
      <div className="grid grid-cols-3 gap-4">
        <Card><Stat label="Closed trades" value={data?.length ?? 0} sub="last 200" /></Card>
        <Card><Stat label="Net result" value={signedUsd(net)} tone={tone(net)} sub="after fees" /></Card>
        <Card><Stat label="Win rate" value={known.length ? `${Math.round((wins / known.length) * 100)}%` : "n/a"} sub={`${wins} of ${known.length}`} /></Card>
      </div>
      <Card title="History">
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorBox message={error} />
        ) : !data?.length ? (
          <Empty>No closed trades yet.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr>
                <Th>Closed</Th>
                <Th>Pair</Th>
                <Th>Side</Th>
                <Th>Strategy</Th>
                <Th right>Entry</Th>
                <Th right>Exit</Th>
                <Th right>Result</Th>
                <Th right>Fees</Th>
                <Th>Why it closed</Th>
                <Th>Mode</Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.map((t, i) => (
                <tr key={`${t.closed_at}-${t.symbol}-${i}`}>
                  <Td className="text-mute">{dateTime(t.closed_at)}</Td>
                  <Td>{shortSymbol(t.symbol)}</Td>
                  <Td><SideBadge side={t.side} /></Td>
                  <Td className="text-mute">{strategyLabel(t.strategy)}</Td>
                  <Td right>{price(t.entry_price)}</Td>
                  <Td right>{price(t.exit_price)}</Td>
                  <Td right tone={tone(t.pnl)}>{t.pnl === null ? "see exchange" : signedUsd(t.pnl)}</Td>
                  <Td right className="text-mute">{usd(t.fees)}</Td>
                  <Td className="text-mute">{t.reason}</Td>
                  <Td><Badge tone={t.mode === "live" ? "warn" : "accent"}>{t.mode === "live" ? "live" : "paper"}</Badge></Td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>
    </>
  );
}
