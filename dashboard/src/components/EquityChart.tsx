import { dateTime, usd } from "@/lib/format";
import type { EquityPoint } from "@/lib/types";

/** Equity curve drawn as plain SVG, so there is no charting dependency to maintain or audit. */
export function EquityChart({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) {
    return (
      <div className="flex h-56 items-center justify-center font-mono text-sm text-mute">
        Not enough history yet. Samples are taken every 5 minutes while the bot runs.
      </div>
    );
  }
  const W = 800;
  const H = 240;
  const padX = 8;
  const padY = 18;
  const values = points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || Math.max(Math.abs(max) * 0.01, 1);
  const lo = min - span * 0.08;
  const hi = max + span * 0.08;
  const t0 = new Date(points[0].at).getTime();
  const tN = new Date(points[points.length - 1].at).getTime();
  const x = (t: number) => padX + ((t - t0) / Math.max(tN - t0, 1)) * (W - padX * 2);
  const y = (v: number) => padY + (1 - (v - lo) / (hi - lo)) * (H - padY * 2);
  const coords = points.map((p) => [x(new Date(p.at).getTime()), y(p.equity)] as const);
  const line = coords.map(([cx, cy], i) => `${i === 0 ? "M" : "L"}${cx.toFixed(1)},${cy.toFixed(1)}`).join(" ");
  const area = `${line} L${coords[coords.length - 1][0].toFixed(1)},${H - padY} L${coords[0][0].toFixed(1)},${H - padY} Z`;
  const first = values[0];
  const last = values[values.length - 1];
  const up = last >= first;
  const stroke = up ? "#26d07c" : "#ff5c6c";
  const baseline = y(first);
  return (
    <figure>
      <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="h-56 w-full" role="img" aria-label={`Equity from ${usd(first)} to ${usd(last)}`}>
        <defs>
          <linearGradient id="eqfill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor={stroke} stopOpacity="0.28" />
            <stop offset="100%" stopColor={stroke} stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((f) => (
          <line key={f} x1={padX} x2={W - padX} y1={padY + f * (H - padY * 2)} y2={padY + f * (H - padY * 2)} stroke="#1c2733" strokeWidth="1" vectorEffect="non-scaling-stroke" />
        ))}
        <line x1={padX} x2={W - padX} y1={baseline} y2={baseline} stroke="#6b7a8c" strokeWidth="1" strokeDasharray="4 4" opacity="0.6" vectorEffect="non-scaling-stroke" />
        <path d={area} fill="url(#eqfill)" />
        <path d={line} fill="none" stroke={stroke} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      </svg>
      <span
        aria-hidden
        className="absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full"
        style={{ left: `${(coords[coords.length - 1][0] / W) * 100}%`, top: `${(coords[coords.length - 1][1] / H) * 100}%`, background: stroke }}
      />
      </div>
      <figcaption className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 font-mono text-xs text-mute">
        <span>{dateTime(points[0].at)}</span>
        <span>
          low <span className="text-ink">{usd(min)}</span> · high <span className="text-ink">{usd(max)}</span>
        </span>
        <span>{dateTime(points[points.length - 1].at)}</span>
      </figcaption>
    </figure>
  );
}
