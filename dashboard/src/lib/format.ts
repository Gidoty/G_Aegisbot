export function usd(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "n/a";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function signedUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "n/a";
  return (n > 0 ? "+" : "") + usd(n);
}

export function pct(fraction: number | null | undefined, digits = 2): string {
  if (fraction === null || fraction === undefined || Number.isNaN(fraction)) return "n/a";
  return `${(fraction * 100).toFixed(digits)}%`;
}

/** Prices span many magnitudes (BTC 60,000 down to PEPE 0.01), so keep about 5 significant digits. */
export function price(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "n/a";
  const abs = Math.abs(n);
  const digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : abs >= 0.01 ? 5 : 7;
  return n.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: digits });
}

export function shortSymbol(symbol: string): string {
  return symbol.split("/")[0];
}

export function tone(n: number | null | undefined): "up" | "down" | "mute" {
  if (n === null || n === undefined || n === 0) return "mute";
  return n > 0 ? "up" : "down";
}

export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function daysLeft(iso: string | null | undefined, now: number = Date.now()): number | null {
  if (!iso) return null;
  return Math.ceil((new Date(iso).getTime() - now) / 86_400_000);
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  const d = new Date(iso);
  return `${d.toISOString().slice(0, 10)} ${d.toISOString().slice(11, 16)} UTC`;
}

/** Human label for the strategy that opened a position. Unknown or missing values are shown as they are, never guessed. */
export function strategyLabel(s: string | null | undefined): string {
  if (!s) return "unknown";
  const names: Record<string, string> = { breakout: "Breakout", trend: "Trend", adopted: "Adopted" };
  return names[s] ?? s;
}
