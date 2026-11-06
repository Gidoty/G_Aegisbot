"use client";

import { useState, type FormEvent } from "react";
import { useMe } from "@/components/Shell";
import { Badge, Button, Card, ErrorBox, Field, inputClass } from "@/components/ui";
import { api, errorText } from "@/lib/api";
import { shortSymbol } from "@/lib/format";
import { DEFAULT_PAIRS, type Me } from "@/lib/types";

function TradingSettings() {
  const { me, reload } = useMe();
  const [leverage, setLeverage] = useState(me.leverage);
  const [pairs, setPairs] = useState<string[]>(me.active_coins);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const options = Array.from(new Set([...DEFAULT_PAIRS, ...me.active_coins]));

  function toggle(p: string) {
    setPairs((cur) => (cur.includes(p) ? cur.filter((x) => x !== p) : [...cur, p]));
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      await api<Me>("/api/me/settings", { method: "PUT", body: { leverage, active_coins: pairs } });
      setMsg({ ok: true, text: "Saved. New trades use these settings; open positions are not changed." });
      reload();
    } catch (err) {
      setMsg({ ok: false, text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Trading settings">
      <form onSubmit={save} className="space-y-5">
        <div>
          <div className="mb-1 font-mono text-[11px] uppercase tracking-[0.14em] text-mute">Leverage</div>
          <div className="flex gap-1.5" role="radiogroup" aria-label="Leverage">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                type="button"
                key={n}
                role="radio"
                aria-checked={leverage === n}
                onClick={() => setLeverage(n)}
                className={`h-9 w-12 rounded border font-mono text-sm ${leverage === n ? "border-accent bg-accent/15 text-accent" : "border-line bg-raised text-ink hover:border-mute"}`}
              >
                {n}x
              </button>
            ))}
          </div>
          <p className="mt-1 text-xs text-mute">Higher leverage means a bigger gain or loss for the same move. 3x is the default.</p>
        </div>
        <fieldset>
          <legend className="mb-1 font-mono text-[11px] uppercase tracking-[0.14em] text-mute">Pairs to trade ({pairs.length} of 15 max)</legend>
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 md:grid-cols-5">
            {options.map((p) => (
              <label key={p} className={`flex cursor-pointer items-center gap-2 rounded border px-2.5 py-1.5 font-mono text-xs ${pairs.includes(p) ? "border-accent/50 bg-accent/10 text-ink" : "border-line text-mute"}`}>
                <input type="checkbox" checked={pairs.includes(p)} onChange={() => toggle(p)} />
                {shortSymbol(p)}
              </label>
            ))}
          </div>
        </fieldset>
        {msg && (msg.ok ? <p className="text-sm text-up">{msg.text}</p> : <ErrorBox message={msg.text} />)}
        <Button type="submit" variant="primary" disabled={busy || pairs.length === 0 || pairs.length > 15}>
          {busy ? "Saving…" : "Save settings"}
        </Button>
      </form>
    </Card>
  );
}

function KeysForm() {
  const { me, reload } = useMe();
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string; warnings?: string[] } | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const r = await api<{ stored: boolean; warnings: string[] }>("/api/me/keys", { method: "PUT", body: { api_key: key, api_secret: secret } });
      setResult({ ok: true, text: "Key checked and saved.", warnings: r.warnings });
      reload();
    } catch (err) {
      setResult({ ok: false, text: errorText(err) });
    } finally {
      setKey("");
      setSecret("");
      setBusy(false);
    }
  }

  return (
    <Card title="Binance API key" right={<Badge tone={me.keys_stored ? "up" : "warn"}>{me.keys_stored ? "stored" : "not set"}</Badge>}>
      <ul className="mb-4 list-disc space-y-1 pl-5 text-sm text-ink/90">
        <li>Create the key with <b>Enable Reading</b> and <b>Enable Futures</b> only.</li>
        <li><b>Never enable withdrawals.</b> A key that can withdraw is rejected.</li>
        <li>Restrict the key to the server&apos;s IP address in Binance.</li>
        <li>Your Binance futures account must be in One-way position mode.</li>
      </ul>
      <form onSubmit={submit} className="space-y-4" autoComplete="off">
        <Field label="API key">
          <input className={inputClass} type="password" autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} />
        </Field>
        <Field label="API secret" hint="Sent over HTTPS, checked with Binance, then stored encrypted. It is never shown again.">
          <input className={inputClass} type="password" autoComplete="off" spellCheck={false} value={secret} onChange={(e) => setSecret(e.target.value)} />
        </Field>
        {result && (result.ok ? (
          <div className="text-sm text-up">
            {result.text}
            {result.warnings?.map((w) => <p key={w} className="mt-1 text-warn">Note: {w}</p>)}
          </div>
        ) : (
          <ErrorBox message={result.text} />
        ))}
        <Button type="submit" variant="primary" disabled={busy || key.length < 16 || secret.length < 16}>
          {busy ? "Checking with Binance…" : me.keys_stored ? "Replace key" : "Save key"}
        </Button>
      </form>
    </Card>
  );
}

export default function SettingsPage() {
  const { me } = useMe();
  return (
    <>
      <Card title="Account">
        <dl className="grid grid-cols-2 gap-y-2 font-mono text-sm sm:grid-cols-4">
          <div><dt className="text-[11px] uppercase tracking-wider text-mute">Login</dt><dd className="break-all">{me.user_id}</dd></div>
          <div><dt className="text-[11px] uppercase tracking-wider text-mute">Mode</dt><dd>{me.trading_mode === "live" ? "Live" : "Paper"}</dd></div>
          <div><dt className="text-[11px] uppercase tracking-wider text-mute">Wallet cap</dt><dd>${me.max_wallet_cap_usdt.toLocaleString()}</dd></div>
          <div><dt className="text-[11px] uppercase tracking-wider text-mute">Role</dt><dd>{me.is_admin ? "Administrator" : "User"}</dd></div>
        </dl>
        <p className="mt-3 text-xs text-mute">The wallet cap and the live/paper mode are set by the administrator.</p>
      </Card>
      <TradingSettings />
      <KeysForm />
    </>
  );
}
