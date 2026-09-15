/**
 * eventsData.tsx — where the AI Detections tab gets its numbers, and the chart
 * it draws them in.
 *
 * `useMotionData` polls the motion service's in-memory ring; `useIndexStats`
 * probes the analytics index (which may be unreachable, and says so rather than
 * showing a zero). The formatters and the hourly chart are here because they
 * only make sense against those two shapes.
 */
import { useEffect, useMemo, useRef, useState } from 'react';

import { ApiError, apiFetch } from '@/lib/api';
import { indexStats } from '@/lib/smartsearch';
import type { MotionCamera } from '@/lib/types';

export interface MotionEvent {
  id: number;
  camera: string;           // motion-service name == registry slug
  started_at: string;
  ended_at: string | null;
}

export const HOUR = 3600_000;
export const RING_SIZE = 1000;     // the motion service's in-memory event log capacity

/* ── Data ────────────────────────────────────────────────────────────────── */

export function useMotionData() {
  const [events, setEvents] = useState<MotionEvent[] | null>(null);
  const [states, setStates] = useState<MotionCamera[]>([]);
  const [down, setDown] = useState(false);
  const alive = useRef(true);

  const refresh = async () => {
    try {
      const d = await apiFetch<{ events: MotionEvent[] }>(`/motion/events?limit=${RING_SIZE}`);
      if (!alive.current) return;
      setEvents(d.events || []);
      setDown(false);
    } catch {
      if (alive.current) { setDown(true); setEvents(e => e ?? []); }
    }
    apiFetch<{ cameras: MotionCamera[] }>('/motion/cameras')
      .then(d => { if (alive.current) setStates(d.cameras || []); })
      .catch(() => {});
  };

  useEffect(() => {
    alive.current = true;
    refresh();
    const t = setInterval(refresh, 10_000);
    return () => { alive.current = false; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { events, states, down, refresh };
}

export type IndexProbe =
  | { kind: 'loading' }
  | { kind: 'denied' }                       // no smart_search capability
  | { kind: 'unreachable' }
  | { kind: 'not-configured' }
  // Counts cover this VMS's cameras only, and are null when the index ignored
  // the camera filter — unknown, which must not render as a number.
  | { kind: 'ok'; people: number | null; vehicles: number | null };

export function useIndexStats(): IndexProbe {
  const [probe, setProbe] = useState<IndexProbe>({ kind: 'loading' });
  useEffect(() => {
    let alive = true;
    const load = () => indexStats()
      .then(s => {
        if (!alive) return;
        setProbe(s.reachable
          ? { kind: 'ok',
              people: s.domains.people?.vectors_count ?? null,
              vehicles: s.domains.vehicles?.vectors_count ?? null }
          : s.configured === false
            ? { kind: 'not-configured' }
            : { kind: 'unreachable' });
      })
      .catch((e: unknown) => {
        if (alive) setProbe(e instanceof ApiError && e.status === 403 ? { kind: 'denied' } : { kind: 'unreachable' });
      });
    load();
    const t = setInterval(load, 60_000);
    return () => { alive = false; clearInterval(t); };
  }, []);
  return probe;
}

/* ── Formatting ──────────────────────────────────────────────────────────── */

/** 1,284 · 12.9K · 4.2M — stat tiles compact past four digits. */
export function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

export function hhmm(d: Date): string {
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** Round up to a clean axis maximum. */
export function niceCeil(n: number): number {
  if (n <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(n));
  for (const m of [1, 2, 4, 5, 10]) {
    if (m * pow >= n) return m * pow;
  }
  return 10 * pow;
}

/* ── Hourly volume chart ─────────────────────────────────────────────────── */

/**
 * 24 one-hour columns, hand-rolled SVG (this codebase ships no chart library).
 * Single series, so a single hue: the cool data slate (--tl-footage), keeping
 * orange as UI accent. Rounded caps on the data end only, hairline grid, value
 * label on the peak column, per-column hover tooltip. The in-progress hour
 * renders lighter so a half-empty bucket doesn't read as a drop-off.
 */
export function HourlyChart({ buckets, windowStartMs }: { buckets: number[]; windowStartMs: number }) {
  const W = 720, H = 190, L = 38, R = 8, T = 16, B = 24;
  const plotW = W - L - R, plotH = H - T - B;
  const slot = plotW / 24;
  const barW = Math.min(22, slot - 6);
  const max = Math.max(...buckets);
  const yMax = niceCeil(max);
  const peakIdx = buckets.indexOf(max);

  const bar = (x: number, y: number, w: number, h: number) => {
    const r = Math.min(4, h);
    return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
  };

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
         role="img" aria-label="Motion events per hour, last 24 hours">
      {/* Hairline grid at half and full scale; baseline carries the axis. */}
      {[0.5, 1].map(f => (
        <line key={f} x1={L} x2={W - R} y1={T + plotH - f * plotH} y2={T + plotH - f * plotH}
              stroke="var(--ch-grid)" strokeWidth={1} />
      ))}
      <line x1={L} x2={W - R} y1={T + plotH} y2={T + plotH} stroke="var(--border2)" strokeWidth={1} />
      {[0, 0.5, 1].map(f => (
        <text key={f} x={L - 7} y={T + plotH - f * plotH + 3.5} textAnchor="end"
              fontSize={10} fill="var(--dim)">{Math.round(f * yMax).toLocaleString()}</text>
      ))}

      {buckets.map((n, i) => {
        const hourStart = new Date(windowStartMs + i * HOUR);
        const hh = hourStart.getHours();
        const inProgress = i === buckets.length - 1;
        const h = n > 0 ? Math.max(2, (n / yMax) * plotH) : 0;
        const x = L + i * slot + (slot - barW) / 2;
        const y = T + plotH - h;
        return (
          <g key={i} className="ev-slot">
            <rect className="ev-hit" x={L + i * slot} y={T} width={slot} height={plotH} rx={3} />
            {n > 0 && (
              <path d={bar(x, y, barW, h)} fill="var(--tl-footage)" opacity={inProgress ? 0.55 : 1} />
            )}
            {i === peakIdx && max > 0 && (
              <text x={x + barW / 2} y={y - 5} textAnchor="middle" fontSize={10.5} fontWeight={600}
                    fill="var(--text2)">{max.toLocaleString()}</text>
            )}
            {hh % 3 === 0 && (
              <text x={L + i * slot + slot / 2} y={H - 8} textAnchor="middle" fontSize={10}
                    fill="var(--dim)">{String(hh).padStart(2, '0')}</text>
            )}
            <title>{`${String(hh).padStart(2, '0')}:00–${String((hh + 1) % 24).padStart(2, '0')}:00 · ${n} event${n === 1 ? '' : 's'}${inProgress ? ' (in progress)' : ''}`}</title>
          </g>
        );
      })}
    </svg>
  );
}

/* ── The tab ─────────────────────────────────────────────────────────────── */
