import type { ButtonHTMLAttributes, ReactNode } from "react";

type Tone = "up" | "down" | "warn" | "accent" | "mute";

const toneText: Record<Tone, string> = {
  up: "text-up",
  down: "text-down",
  warn: "text-warn",
  accent: "text-accent",
  mute: "text-mute",
};

const toneBadge: Record<Tone, string> = {
  up: "border-up/40 bg-up/10 text-up",
  down: "border-down/40 bg-down/10 text-down",
  warn: "border-warn/40 bg-warn/10 text-warn",
  accent: "border-accent/40 bg-accent/10 text-accent",
  mute: "border-line bg-raised text-mute",
};

export function Card({ title, right, children, className = "" }: { title?: string; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`min-w-0 rounded-md border border-line bg-panel ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="font-mono text-[11px] uppercase tracking-[0.18em] text-mute">{title}</h2>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone = "mute" }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  const valueClass = tone === "mute" ? "text-ink" : toneText[tone];
  return (
    <div>
      <div className="font-mono text-[11px] uppercase tracking-[0.18em] text-mute">{label}</div>
      <div className={`mt-1 font-mono text-2xl font-semibold tabular-nums ${valueClass}`}>{value}</div>
      {sub && <div className="mt-0.5 font-mono text-xs text-mute">{sub}</div>}
    </div>
  );
}

export function Badge({ tone = "mute", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded border px-1.5 py-0.5 font-mono text-[10px] font-semibold uppercase tracking-wider ${toneBadge[tone]}`}>
      {children}
    </span>
  );
}

export function Banner({ tone, title, children }: { tone: "warn" | "down" | "accent"; title: string; children?: ReactNode }) {
  return (
    <div role="alert" className={`rounded-md border px-4 py-3 ${toneBadge[tone]}`}>
      <div className="font-mono text-xs font-semibold uppercase tracking-wider">{title}</div>
      {children && <div className="mt-1 text-sm text-ink/90">{children}</div>}
    </div>
  );
}

export function Button({
  variant = "default",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "danger" | "ghost" }) {
  const styles = {
    default: "border-line bg-raised text-ink hover:border-mute",
    primary: "border-accent/60 bg-accent/15 text-accent hover:bg-accent/25",
    danger: "border-down/60 bg-down/15 text-down hover:bg-down/25",
    ghost: "border-transparent bg-transparent text-mute hover:text-ink",
  }[variant];
  return (
    <button
      {...props}
      className={`rounded border px-3 py-1.5 font-mono text-xs font-semibold uppercase tracking-wider transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40 ${styles} ${className}`}
    />
  );
}

export const inputClass =
  "w-full rounded border border-line bg-bg px-3 py-2 font-mono text-sm text-ink placeholder:text-mute/60 focus:border-accent focus:outline-none focus-visible:ring-1 focus-visible:ring-accent";

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block font-mono text-[11px] uppercase tracking-[0.14em] text-mute">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-mute">{hint}</span>}
    </label>
  );
}

export function Th({ children, right }: { children?: ReactNode; right?: boolean }) {
  return (
    <th className={`whitespace-nowrap px-3 py-2 font-mono text-[11px] font-medium uppercase tracking-[0.14em] text-mute ${right ? "text-right" : "text-left"}`}>
      {children}
    </th>
  );
}

export function Td({ children, right, tone, className = "" }: { children?: ReactNode; right?: boolean; tone?: Tone; className?: string }) {
  return (
    <td className={`whitespace-nowrap px-3 py-2 font-mono text-sm tabular-nums ${right ? "text-right" : ""} ${tone ? toneText[tone] : "text-ink"} ${className}`}>
      {children}
    </td>
  );
}

export function TableWrap({ children }: { children: ReactNode }) {
  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full min-w-[520px] border-collapse divide-y divide-line">{children}</table>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="py-8 text-center font-mono text-sm text-mute">{children}</div>;
}

export function Loading() {
  return <div className="py-8 text-center font-mono text-sm text-mute animate-pulse">loading…</div>;
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div role="alert" className="rounded border border-down/40 bg-down/10 px-3 py-2 text-sm text-down">
      {message}
    </div>
  );
}

export function SideBadge({ side }: { side: "long" | "short" }) {
  return <Badge tone={side === "long" ? "up" : "down"}>{side}</Badge>;
}
