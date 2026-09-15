/**
 * healthChart.tsx — the availability chart HealthTab draws, and the maths
 * behind it.
 *
 * Two views of one dataset on a shared time axis: stacked columns that
 * aggregate each bucket's up/down/no-data split (so a 40-second outage in a
 * 30-day window is still a readable percentage), and an exact-resolution
 * ribbon underneath with a 1px floor on down spans, so no outage can disappear
 * between pixels.
 *
 * Split out of HealthTab because it is geometry, not page logic: the padding
 * constants, the bucketiser and the incident merge are only meaningful to each
 * other.
 */
import { useEffect, useMemo, useRef, useState } from 'react';

import { fmtDuration } from '@/lib/format';

export interface UptimeInterval { start: string; end: string; status: string }
export interface UptimeData {
  from: string;
  to: string;
  uptime_pct: number | null;
  outages: number;
  down_seconds: number;
  unknown_seconds: number;
  intervals: UptimeInterval[];
}

/** Three display states — the API's five statuses collapse onto these. */
export type State = 'up' | 'down' | 'void';
export const stateOf = (s: string): State =>
  s === 'connected' ? 'up' : (s === 'disconnected' || s === 'error') ? 'down' : 'void';
export const FILL: Record<State, string> = {
  up: 'var(--ch-up)', down: 'var(--ch-down)', void: 'var(--ch-void)',
};
export const STATE_LABEL: Record<State, string> = { up: 'Up', down: 'Down', void: 'No data' };

/** [hours, label, bucket count] — 24×1h, 28×6h, 30×1d. */
export const RANGES: [number, string, number][] = [[24, '24h', 24], [168, '7d', 28], [720, '30d', 30]];

// Plot geometry. The container's height is plot + ribbon + axis so the axis
// labels are never the thing that gets cut off.
export const PAD_T = 10, PLOT_H = 196, GAP = 16, RIBBON_H = 24, AXIS_H = 22;
export const PAD_L = 38, PAD_R = 8;
export const SVG_H = PAD_T + PLOT_H + GAP + RIBBON_H + AXIS_H;
export const RIBBON_Y = PAD_T + PLOT_H + GAP;
export const SEG_GAP = 2;   // surface gap between stacked segments
export const BAR_MAX = 26;

export interface Bucket { t0: number; t1: number; up: number; down: number; void: number }

/** Split the window into `n` equal buckets and pour each interval into them. */
export function bucketize(data: UptimeData, n: number): Bucket[] {
  const t0 = new Date(data.from).getTime();
  const t1 = new Date(data.to).getTime();
  const len = Math.max((t1 - t0) / n, 1);
  const out: Bucket[] = Array.from({ length: n }, (_, i) => ({
    t0: t0 + len * i, t1: t0 + len * (i + 1), up: 0, down: 0, void: 0,
  }));
  for (const iv of data.intervals) {
    const s = new Date(iv.start).getTime(), e = new Date(iv.end).getTime();
    const key = stateOf(iv.status);
    let i = Math.max(0, Math.floor((s - t0) / len));
    for (; i < n && out[i].t0 < e; i++) {
      const ms = Math.min(e, out[i].t1) - Math.max(s, out[i].t0);
      if (ms > 0) out[i][key] += ms;
    }
  }
  return out;
}

/** Rect with rounded top corners and a square baseline (marks-and-anatomy). */
export function topRounded(x: number, y: number, w: number, h: number, r: number): string {
  const rad = Math.max(0, Math.min(r, w / 2, h));
  return `M${x} ${y + h}V${y + rad}a${rad} ${rad} 0 0 1 ${rad} ${-rad}h${w - rad * 2}` +
    `a${rad} ${rad} 0 0 1 ${rad} ${rad}V${y + h}Z`;
}

/** A dropout as an operator would count it. The backend logs every status
    transition, so one flapping camera writes a long run of short
    disconnected/error rows; anything reconnecting for less than a minute is
    the same incident still in progress, not a new one. */
export const MERGE_GAP_MS = 60_000;
export interface Incident { start: number; end: number; parts: number }

export function incidentsOf(intervals: UptimeInterval[]): Incident[] {
  const out: Incident[] = [];
  for (const iv of intervals) {
    if (stateOf(iv.status) !== 'down') continue;
    const s = new Date(iv.start).getTime(), e = new Date(iv.end).getTime();
    const last = out[out.length - 1];
    if (last && s - last.end <= MERGE_GAP_MS) {
      last.end = Math.max(last.end, e);
      last.parts++;
    } else {
      out.push({ start: s, end: e, parts: 1 });
    }
  }
  return out;
}

/** Axis unit that keeps the numbers small — seconds, minutes, or hours. */
export function unitFor(maxMs: number) {
  if (maxMs >= 2 * 3600_000) return { div: 3600_000, suffix: 'h' };
  if (maxMs >= 90_000) return { div: 60_000, suffix: 'm' };
  return { div: 1000, suffix: 's' };
}
export const NICE = [1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600];
const niceCeil = (v: number) => NICE.find(n => n >= v) ?? Math.ceil(v / 60) * 60;

/** Element width, tracked live — the columns need real pixels, not a viewBox
    stretch (a stretched viewBox would distort the 2px gaps and the corners). */
export function useWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(e => setW(e[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

export interface Tip { x: number; y: number; title: string; rows: [string, string][] }

export function Tile({ label, value, sub, mono }: {
  label: string; value: React.ReactNode; sub?: React.ReactNode; mono?: boolean;
}) {
  return (
    <div className="ch-tile">
      <div className="ch-lbl">{label}</div>
      <div className="ch-val" style={mono ? { fontFamily: 'var(--mono)', fontSize: 18 } : undefined}>{value}</div>
      {sub != null && <div className="ch-sub">{sub}</div>}
    </div>
  );
}

/**
 * Downtime pattern — absolute minutes lost per bucket, on its own y-scale.
 *
 * This is not a duplicate of the availability columns above it. Those are
 * proportional, so on a 99.8%-uptime camera every outage is a sub-pixel sliver;
 * here the axis tops out at the worst bucket, which is what makes a scatter of
 * short dropouts legible as a *pattern* (nightly? one bad afternoon?) instead
 * of a hundred table rows nobody reads.
 */
export function DowntimeChart({ buckets, incidents, fmtTick, spanLabel }: {
  buckets: Bucket[]; incidents: Incident[];
  fmtTick: (t: number) => string; spanLabel: string;
}) {
  const [wrapRef, w] = useWidth<HTMLDivElement>();
  const [tip, setTip] = useState<Tip | null>(null);

  const H = 132, TOP = 18, AX = 20;
  const innerW = Math.max(w - 32 - PAD_L - PAD_R, 10);
  const band = innerW / Math.max(buckets.length, 1);
  const barW = Math.min(BAR_MAX, Math.max(band - 5, 3));

  const maxDown = buckets.reduce((m, b) => Math.max(m, b.down), 0);
  const unit = unitFor(maxDown);
  const top = niceCeil(maxDown / unit.div) * unit.div;
  const worstIdx = buckets.findIndex(b => b.down === maxDown);
  const fmtAxis = (ms: number) => `${+(ms / unit.div).toFixed(ms % unit.div ? 1 : 0)}${unit.suffix}`;
  const ticks = buckets.length
    ? Array.from({ length: 7 }, (_, i) =>
        buckets[0].t0 + ((buckets[buckets.length - 1].t1 - buckets[0].t0) * i) / 6)
    : [];

  if (maxDown <= 0) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 9, padding: '26px 2px', fontSize: 13 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--ch-up)' }} />
        <span>No downtime recorded in the {spanLabel} window — the stream never dropped.</span>
      </div>
    );
  }

  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      <svg width={Math.max(w - 32, 10)} height={TOP + H + AX} style={{ display: 'block', overflow: 'visible' }}
        role="img" aria-label={`Downtime per ${spanLabel} bucket, worst bucket ${fmtAxis(maxDown)}`}>
        {[0, 0.5, 1].map(f => {
          const y = TOP + H - f * H;
          return (
            <g key={f}>
              <line x1={PAD_L} x2={PAD_L + innerW} y1={y} y2={y} stroke="var(--ch-grid)" strokeWidth={1} />
              <text x={PAD_L - 8} y={y + 3.5} textAnchor="end" fill="var(--dim)" fontSize={10}
                style={{ fontVariantNumeric: 'tabular-nums' }}>{f === 0 ? '0' : fmtAxis(top * f)}</text>
            </g>
          );
        })}

        {buckets.map((b, i) => {
          const x = PAD_L + band * i + (band - barW) / 2;
          // A 2px floor: a bucket that lost 20 seconds out of a day must still
          // put ink on the chart, or "rare but real" reads as "never".
          const h = b.down > 0 ? Math.max((b.down / top) * H, 2) : 0;
          const n = incidents.filter(v => v.start >= b.t0 && v.start < b.t1).length;
          return (
            <g key={i}>
              {h > 0 && <path d={topRounded(x, TOP + H - h, barW, h, 4)} fill="var(--ch-down)" />}
              <rect x={PAD_L + band * i} y={TOP} width={band} height={H} fill="transparent"
                style={{ cursor: 'crosshair' }}
                onMouseLeave={() => setTip(null)}
                onMouseEnter={() => setTip({
                  x: x + barW / 2, y: TOP + H - h - 6,
                  title: `${fmtTick(b.t0)} — ${fmtTick(b.t1)}`,
                  rows: [
                    ['Down', b.down > 0 ? fmtDuration(b.down) : 'none'],
                    ['Incidents', String(n)],
                  ],
                })} />
            </g>
          );
        })}

        {/* Direct-label the extreme only — a number on every column is noise. */}
        {worstIdx >= 0 && (
          <text x={PAD_L + band * worstIdx + band / 2}
            y={TOP + H - Math.max((maxDown / top) * H, 2) - 7}
            textAnchor="middle" fill="var(--text2)" fontSize={11} fontWeight={600}>
            {fmtDuration(maxDown)}
          </text>
        )}

        {ticks.map((t, i) => (
          <text key={i} x={PAD_L + (innerW * i) / 6} y={TOP + H + 15}
            textAnchor={i === 0 ? 'start' : i === 6 ? 'end' : 'middle'}
            fill="var(--dim)" fontSize={10} style={{ fontVariantNumeric: 'tabular-nums' }}>
            {i === 6 ? 'now' : fmtTick(t)}
          </text>
        ))}
      </svg>

      {tip && (
        <div className="ch-tip" style={{
          left: Math.min(Math.max(tip.x + 16, 90), Math.max(w - 90, 90)),
          top: Math.max(tip.y + 4, 8),
        }}>
          <b>{tip.title}</b>
          {tip.rows.map(([k, v]) => (
            <div className="ch-tip-row" key={k}><span>{k}</span><span>{v}</span></div>
          ))}
        </div>
      )}
    </div>
  );
}
