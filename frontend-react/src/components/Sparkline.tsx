/** Sparkline.tsx — the inventory's Uptime 7d polyline (per-day buckets). */
import type { UptimeEntry } from '@/lib/types';

export function up7Color(pct: number): string {
  return pct >= 99 ? 'var(--green)' : pct >= 90 ? 'var(--yellow)' : 'var(--red)';
}

export function Sparkline({ entry, withPct = false }: { entry?: UptimeEntry; withPct?: boolean }) {
  const pct = entry?.pct;
  if (pct == null) return <span style={{ color: 'var(--dim)' }}>—</span>;
  const s = entry?.series || [];
  const hasSeries = s.some(v => v != null);
  const points = s.map((v, i) => {
    const y = v == null ? 19 : 19 - (v / 100) * 16;
    return `${(i / Math.max(s.length - 1, 1)) * 78 + 1},${y.toFixed(1)}`;
  }).join(' ');
  const line = hasSeries && (
    <svg viewBox="0 0 80 20" style={{ width: 80, height: 20, verticalAlign: 'middle' }}>
      <polyline points={points} fill="none" stroke={up7Color(pct)} strokeWidth={1.5} />
    </svg>
  );
  if (withPct) {
    return (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        {line}<b style={{ color: up7Color(pct), fontSize: 11.5 }}>{pct}%</b>
      </span>
    );
  }
  return <span title={`${pct}% over 7 days`}>{line || <b style={{ color: up7Color(pct), fontSize: 11.5 }}>{pct}%</b>}</span>;
}
