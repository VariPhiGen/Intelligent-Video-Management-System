/**
 * DistributionChart — one People Distribution day as a time chart.
 *
 * The x-axis is always the whole local day (00:00–24:00), so today's chart
 * fills in from the left as the day goes by and a past day reads the same way.
 * Each bucket is plotted over the span it covers: a line point at its middle, a
 * bar across most of its width. A bucket with no samples is a GAP (camera not
 * reporting), never drawn as zero.
 *
 * Same hand-drawn SVG idiom and geometry helpers as the Entry / Exit chart.
 */
import { useEffect, useRef, useState } from 'react';
import { clockLabel, countScale, monotonePath, timeTicks, type Pt } from './events/entryExitGeometry';

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
  /** line = smooth line with points; bar = column per bucket; mark = a tick per bucket. */
  kind: 'line' | 'bar' | 'mark';
}

export interface ChartPoint {
  startMs: number;
  endMs: number;
  /** Tooltip title, e.g. "09:00–10:00". */
  span: string;
  values: Record<string, number | null>;
}

const PAD_L = 52, PAD_R = 16, TOP = 16, PLOT_H = 200, AXIS_H = 26;
const SVG_H = TOP + PLOT_H + AXIS_H;
const MIN_W = 280;
const DAY_MIN = 24 * 60;

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(e => setW(e[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w] as const;
}

/** Round tick steps for a duration axis, in seconds. */
const DURATION_STEPS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400];

export function durationScale(max: number): { top: number; ticks: number[] } {
  const raw = Math.max(max, 20) / 4;
  const step = DURATION_STEPS.find(s => s >= raw) ?? 14400;
  const top = step * Math.ceil(Math.max(max, 20) / step);
  return { top, ticks: Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step) };
}

/** Runs of consecutive non-null values, so a line breaks where reporting stopped. */
function runs(pts: (Pt | null)[]): Pt[][] {
  const out: Pt[][] = [];
  let cur: Pt[] = [];
  for (const p of pts) {
    if (p) cur.push(p);
    else if (cur.length) { out.push(cur); cur = []; }
  }
  if (cur.length) out.push(cur);
  return out;
}

export function DistributionChart({
  dayStartMs, points, series, scale, format, label, empty,
}: {
  dayStartMs: number;
  points: ChartPoint[];
  series: ChartSeries[];
  scale: 'count' | 'duration';
  format: (v: number) => string;
  label: string;
  empty?: string;
}) {
  const [wrapRef, measured] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const w = Math.max(measured, MIN_W);
  const innerW = w - PAD_L - PAD_R;
  const base = TOP + PLOT_H;
  const dayEndMs = dayStartMs + DAY_MIN * 60_000;
  const xAt = (ms: number) => PAD_L + ((ms - dayStartMs) / (dayEndMs - dayStartMs)) * innerW;

  const max = Math.max(0, ...points.flatMap(p => series.map(s => p.values[s.key] ?? 0)));
  const { top, ticks } = scale === 'count' ? countScale(max) : durationScale(max);
  const yOf = (v: number) => base - (v / top) * PLOT_H;
  const xTicks = timeTicks(dayStartMs, dayEndMs, DAY_MIN);
  const hasData = points.some(p => series.some(s => p.values[s.key] != null));

  const bars = series.filter(s => s.kind === 'bar');
  const slot = (p: ChartPoint) => {
    const x0 = xAt(p.startMs), x1 = xAt(p.endMs);
    const width = Math.max(1, (x1 - x0) * 0.72);
    return { x0: x0 + (x1 - x0 - width) / 2, width };
  };

  function onMove(e: React.MouseEvent<SVGRectElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    const ms = dayStartMs + ((e.clientX - rect.left) / Math.max(rect.width, 1)) * (dayEndMs - dayStartMs);
    const i = points.findIndex(p => ms >= p.startMs && ms < p.endMs);
    setHover(i >= 0 ? i : null);
  }
  const hp = hover != null ? points[hover] : null;
  const hx = hp ? (xAt(hp.startMs) + xAt(hp.endMs)) / 2 : 0;

  return (
    <div ref={wrapRef} className="ee-plot pd-plot">
      <div className="ch-legend">
        {series.map(s => (
          <span key={s.key}><i style={{ background: s.color }} />{s.label}</span>
        ))}
      </div>
      <svg width={w} height={SVG_H} viewBox={`0 0 ${w} ${SVG_H}`}
           style={{ display: 'block', maxWidth: '100%' }} role="img" aria-label={label}>
        {ticks.map(t => (
          <g key={t}>
            <line x1={PAD_L} x2={PAD_L + innerW} y1={yOf(t)} y2={yOf(t)}
                  stroke={t === 0 ? 'var(--border2)' : 'var(--ch-grid)'} strokeWidth={1} />
            <text x={PAD_L - 8} y={yOf(t) + 3.5} textAnchor="end" fill="var(--dim)" fontSize={10}
                  style={{ fontVariantNumeric: 'tabular-nums' }}>{format(t)}</text>
          </g>
        ))}

        {bars.map((s, bi) => points.map(p => {
          const v = p.values[s.key];
          if (v == null) return null;
          const { x0, width } = slot(p);
          const bw = width / bars.length;
          return (
            <rect key={`${s.key}-${p.startMs}`} data-series={s.key}
                  x={x0 + bi * bw} y={yOf(v)} width={Math.max(1, bw - 1)} height={Math.max(0, base - yOf(v))}
                  rx={Math.min(3, bw / 4)} fill={s.color} fillOpacity={0.85} />
          );
        }))}

        {series.filter(s => s.kind === 'line').map(s => {
          const pts = points.map(p => {
            const v = p.values[s.key];
            return v == null ? null : [(xAt(p.startMs) + xAt(p.endMs)) / 2, yOf(v)] as Pt;
          });
          return (
            <g key={s.key} data-series={s.key}>
              {runs(pts).map((r, i) => (
                <path key={i} d={monotonePath(r)} fill="none" stroke={s.color} strokeWidth={2}
                      strokeLinejoin="round" strokeLinecap="round" />
              ))}
              {pts.length <= 96 && pts.map((p, i) => p && (
                <circle key={i} cx={p[0]} cy={p[1]} r={2.5} fill={s.color} />
              ))}
            </g>
          );
        })}

        {series.filter(s => s.kind === 'mark').map(s => (
          <g key={s.key} data-series={s.key}>
            {points.map(p => {
              const v = p.values[s.key];
              if (v == null) return null;
              const { x0, width } = slot(p);
              return (
                <line key={p.startMs} x1={x0} x2={x0 + width} y1={yOf(v)} y2={yOf(v)}
                      stroke={s.color} strokeWidth={2.5} strokeLinecap="round" />
              );
            })}
          </g>
        ))}

        {xTicks.map(t => {
          const x = xAt(t);
          const anchor = x - PAD_L < 16 ? 'start' : PAD_L + innerW - x < 16 ? 'end' : 'middle';
          return (
            <g key={t}>
              <line x1={x} x2={x} y1={base} y2={base + 4} stroke="var(--border2)" strokeWidth={1} />
              <text x={x} y={base + 17} textAnchor={anchor} fill="var(--dim)" fontSize={10}
                    style={{ fontVariantNumeric: 'tabular-nums' }}>
                {t >= dayEndMs ? '24:00' : clockLabel(t)}
              </text>
            </g>
          );
        })}

        {!hasData && (
          <text x={PAD_L + innerW / 2} y={TOP + PLOT_H / 2} textAnchor="middle"
                fill="var(--muted)" fontSize={12}>{empty ?? 'No data for this day'}</text>
        )}

        {hp && (
          <line x1={hx} x2={hx} y1={TOP} y2={base} stroke="var(--border3)" strokeWidth={1}
                strokeDasharray="3 3" pointerEvents="none" />
        )}
        <rect x={PAD_L} y={TOP} width={innerW} height={PLOT_H} fill="transparent"
              style={{ cursor: 'crosshair' }} onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
      </svg>

      {hp && (
        <div className="ch-tip" style={{ left: Math.min(Math.max(hx, 76), w - 76), top: 64 }}>
          <b>{hp.span}</b>
          {series.map(s => (
            <div className="ch-tip-row" key={s.key}>
              <span><i className="ee-swatch" style={{ background: s.color }} />{s.label}</span>
              <span>{hp.values[s.key] == null ? 'no data' : format(hp.values[s.key]!)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
