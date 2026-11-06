"use client";

import { useState } from "react";
import { useMe } from "@/components/Shell";
import { Badge, Banner, Button, Card, Empty, ErrorBox, Loading, TableWrap, Td, Th } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { dateTime, daysLeft, usd } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { Billing } from "@/lib/types";

const STATUS_TONE: Record<string, "up" | "warn" | "down" | "mute"> = {
  finished: "up",
  confirming: "warn",
  confirmed: "warn",
  sending: "warn",
  waiting: "mute",
  created: "mute",
  partially_paid: "warn",
  failed: "down",
  expired: "down",
  refunded: "down",
};

export default function BillingPage() {
  const { me } = useMe();
  const { data, error, loading, reload } = useApi<Billing>("/api/me/billing", 30_000);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const params = typeof window === "undefined" ? null : new URLSearchParams(window.location.search);
  const left = daysLeft(me.subscription.expires_at);

  async function pay() {
    setBusy(true);
    setErr(null);
    try {
      const r = await api<{ invoice_url: string }>("/api/me/billing/invoice", { method: "POST" });
      window.location.href = r.invoice_url;
    } catch (e) {
      setErr(errorText(e));
      setBusy(false);
    }
  }

  return (
    <>
      {params?.get("paid") === "1" && (
        <Banner tone="accent" title="Thanks">
          Your payment is being confirmed on the blockchain. Your subscription extends automatically when it completes, usually within minutes.
        </Banner>
      )}
      <Card title="Subscription">
        {me.is_admin ? (
          <p className="text-sm">Administrator accounts do not need a subscription.</p>
        ) : (
          <p className="text-sm">
            Status: <b className={me.subscription.active ? "text-up" : "text-warn"}>{me.subscription.active ? "active" : "inactive"}</b>
            {me.subscription.expires_at && <> · {left !== null && left > 0 ? `${left} days left` : "ended"} ({dateTime(me.subscription.expires_at)})</>}
          </p>
        )}
        {data && !me.is_admin && (
          <div className="mt-4">
            {data.enabled ? (
              <>
                <p className="mb-3 text-sm text-ink/90">
                  {usd(data.price_usd, 2)} for {data.days} days, paid in cryptocurrency. The time is added on top of what you have left.
                </p>
                <Button variant="primary" onClick={pay} disabled={busy}>
                  {busy ? "Opening payment page…" : "Pay with crypto"}
                </Button>
              </>
            ) : (
              <p className="text-sm text-mute">Online payment is not enabled. Ask the administrator to extend your subscription.</p>
            )}
            {err && <div className="mt-3"><ErrorBox message={err} /></div>}
          </div>
        )}
      </Card>
      <Card title="Payments" right={<Button variant="ghost" onClick={reload}>Refresh</Button>}>
        {loading ? (
          <Loading />
        ) : error ? (
          <ErrorBox message={error} />
        ) : !data?.payments.length ? (
          <Empty>No payments yet.</Empty>
        ) : (
          <TableWrap>
            <thead>
              <tr><Th>Date</Th><Th right>Amount</Th><Th right>Days</Th><Th>Status</Th><Th>Credited</Th></tr>
            </thead>
            <tbody className="divide-y divide-line">
              {data.payments.map((p) => (
                <tr key={p.order_id}>
                  <Td className="text-mute">{dateTime(p.created_at)}</Td>
                  <Td right>{usd(p.price_usd)}</Td>
                  <Td right>{p.days}</Td>
                  <Td><Badge tone={STATUS_TONE[p.status] ?? "mute"}>{p.status.replace("_", " ")}</Badge></Td>
                  <Td tone={p.credited ? "up" : "mute"}>{p.credited ? "yes" : "no"}</Td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>
    </>
  );
}
