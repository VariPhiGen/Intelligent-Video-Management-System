/**
 * EntryExitChart — the Entry / Exit graph: Entry and Exit crossings per time
 * bucket as two smooth, filled lines on one time axis, with a hover readout.
 *
 * Drawn in real pixels from the measured width, in the same hand-drawn SVG
 * idiom as the camera Health charts; the geometry lives in entryExitGeometry.ts.
 * Each bucket is plotted at its middle, so a point sits over the span it counts.
 */
import { useEffect, useRef, useState } from 'react';
import type { EntryExitStats } from '@/lib/aiEvents';
import {
  areaPath, bucketLabel, clockLabel, countScale, monotonePath, timeTicks, type Pt,
} from './entryExitGeometry';

export const SERIES = [
  { key: 'entry', label: 'Entry', color: 'var(--ee-entry)' },
  { key: 'exit', label: 'Exit', color: 'var(--ee-exit)' },
] as const;

const PAD_L = 40, PAD_R = 16, TOP = 16, PLOT_H = 220, AXIS_H = 26;
const SVG_H = TOP + PLOT_H + AXIS_H;
const MIN_W = 280;

/** Element width, tracked live. */
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

/** "2 minutes", "10 seconds", "1 hour". */
export function bucketWords(seconds: number): string {
  const [n, unit] = seconds < 60 ? [seconds, 'second'] : seconds < 3600 ? [seconds / 60, 'minute'] : [seconds / 3600, 'hour'];
  return n === 1 ? unit : `${n} ${unit}s`;
}

export function EntryExitChart({ stats }: { stats: EntryExitStats }) {
  const [wrapRef, measured] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const w = Math.max(measured, MIN_W);
  const innerW = w - PAD_L - PAD_R;
  const base = TOP + PLOT_H;
  const { buckets, bucket_seconds: bucketS } = stats;
  const n = buckets.length;
  const startMs = Date.parse(stats.start);
  const endMs = Date.parse(stats.end);
  const span = Math.max(endMs - startMs, 1);

  const xAt = (ms: number) => PAD_L + ((ms - startMs) / span) * innerW;
  const xOf = (i: number) => xAt(Date.parse(buckets[i].start) + bucketS * 500);
  const { top, ticks } = countScale(Math.max(0, ...buckets.map(b => Math.max(b.entry, b.exit))));
  const yOf = (v: number) => base - (v / top) * PLOT_H;

  const lines = SERIES.map(s => {
    const pts: Pt[] = buckets.map((b, i) => [xOf(i), yOf(b[s.key])]);
    return { ...s, line: monotonePath(pts), area: areaPath(pts, base) };
  });
  const xTicks = timeTicks(startMs, endMs, stats.minutes);

  function onMove(e: React.MouseEvent<SVGRectElement>) {
    if (!n) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const i = Math.floor(((e.clientX - rect.left) / Math.max(rect.width, 1)) * n);
    setHover(Math.min(Math.max(i, 0), n - 1));
  }

  const hb = hover != null && hover < n ? buckets[hover] : null;
  const hx = hb ? xOf(hover!) : 0;

  return (
    <div ref={wrapRef} className="ee-plot">
      <svg width={w} height={SVG_H} viewBox={`0 0 ${w} ${SVG_H}`} style={{ display: 'block', maxWidth: '100%' }}
           role="img"
           aria-label={`Entry and Exit crossings per ${bucketWords(bucketS)}: ` +
                       `${stats.totals.entry} entries and ${stats.totals.exit} exits`}>
        {ticks.map(t => (
          <g key={t}>
            <line x1={PAD_L} x2={PAD_L + innerW} y1={yOf(t)} y2={yOf(t)}
                  stroke={t === 0 ? 'var(--border2)' : 'var(--ch-grid)'} strokeWidth={1} />
            <text x={PAD_L - 8} y={yOf(t) + 3.5} textAnchor="end" fill="var(--dim)" fontSize={10}
                  style={{ fontVariantNumeric: 'tabular-nums' }}>{t}</text>
          </g>
        ))}

        {lines.map(l => (
          <path key={`${l.key}-area`} data-series={l.key} data-part="area" d={l.area}
                fill={l.color} fillOpacity={0.14} stroke="none" />
        ))}
        {lines.map(l => (
          <path key={`${l.key}-line`} data-series={l.key} data-part="line" d={l.line}
                fill="none" stroke={l.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        ))}

        {xTicks.map(t => {
          const x = xAt(t);
          const anchor = x - PAD_L < 16 ? 'start' : PAD_L + innerW - x < 16 ? 'end' : 'middle';
          return (
            <g key={t}>
              <line x1={x} x2={x} y1={base} y2={base + 4} stroke="var(--border2)" strokeWidth={1} />
              <text x={x} y={base + 17} textAnchor={anchor} fill="var(--dim)" fontSize={10}
                    style={{ fontVariantNumeric: 'tabular-nums' }}>{clockLabel(t)}</text>
            </g>
          );
        })}

        {hb && (
          <g pointerEvents="none">
            <line x1={hx} x2={hx} y1={TOP} y2={base} stroke="var(--border3)" strokeWidth={1} strokeDasharray="3 3" />
            {SERIES.map(s => (
              <circle key={s.key} cx={hx} cy={yOf(hb[s.key])} r={3.5}
                      fill="var(--surface)" stroke={s.color} strokeWidth={2} />
            ))}
          </g>
        )}
        <rect x={PAD_L} y={TOP} width={innerW} height={PLOT_H} fill="transparent"
              style={{ cursor: 'crosshair' }} onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
      </svg>

      {hb && (
        <div className="ch-tip" style={{
          left: Math.min(Math.max(hx, 76), w - 76),
          top: Math.max(Math.min(yOf(hb.entry), yOf(hb.exit)) - 10, 72),
        }}>
          <b>{bucketLabel(hb.start, bucketS)}</b>
          {SERIES.map(s => (
            <div className="ch-tip-row" key={s.key}>
              <span><i className="ee-swatch" style={{ background: s.color }} />{s.label}</span>
              <span>{hb[s.key]}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
