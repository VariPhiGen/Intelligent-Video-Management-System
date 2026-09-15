/**
 * entryExitGeometry.ts — the geometry of the Entry / Exit graph: its count
 * scale, its smooth lines and the filled areas under them, and its time axis.
 *
 * The lines are monotone cubic curves (Fritsch–Carlson) through each bucket's
 * count. They are smooth, but a curve never overshoots the buckets it joins: it
 * never dips below zero, never peaks above the busiest bucket, and a quiet
 * stretch lies flat on the baseline. No steps, no blocks.
 */

export type Pt = readonly [number, number];

const r2 = (v: number) => +v.toFixed(2);

/** The y-axis: a round top at or above `max`, and whole-number ticks from 0 to it. */
export function countScale(max: number): { top: number; ticks: number[] } {
  const m = Math.max(max, 4);
  const raw = m / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, [1, 2, 5, 10].map(f => f * mag).find(s => s >= raw) ?? 10 * mag);
  const top = step * Math.ceil(m / step);
  return { top, ticks: Array.from({ length: Math.round(top / step) + 1 }, (_, i) => i * step) };
}

/** A smooth line through `pts` (x ascending), as SVG path data. */
export function monotonePath(pts: readonly Pt[]): string {
  const n = pts.length;
  if (n === 0) return '';
  if (n === 1) return `M${r2(pts[0][0])},${r2(pts[0][1])}`;
  const dx: number[] = [];
  const slope: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0];
    slope[i] = dx[i] ? (pts[i + 1][1] - pts[i][1]) / dx[i] : 0;
  }
  const m: number[] = new Array(n);
  m[0] = slope[0];
  m[n - 1] = slope[n - 2];
  for (let i = 1; i < n - 1; i++) {
    m[i] = slope[i - 1] * slope[i] <= 0 ? 0 : (slope[i - 1] + slope[i]) / 2;
  }
  for (let i = 0; i < n - 1; i++) {
    if (slope[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
    const a = m[i] / slope[i], b = m[i + 1] / slope[i], h = a * a + b * b;
    if (h > 9) {
      const t = 3 / Math.sqrt(h);
      m[i] = t * a * slope[i];
      m[i + 1] = t * b * slope[i];
    }
  }
  let path = `M${r2(pts[0][0])},${r2(pts[0][1])}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i];
    const [x1, y1] = pts[i + 1];
    const third = dx[i] / 3;
    path += `C${r2(x0 + third)},${r2(y0 + m[i] * third)},${r2(x1 - third)},${r2(y1 - m[i + 1] * third)},${r2(x1)},${r2(y1)}`;
  }
  return path;
}

/** The same line closed down to the baseline: the filled mountain under it. */
export function areaPath(pts: readonly Pt[], baseY: number): string {
  if (pts.length < 2) return '';
  const last = pts[pts.length - 1];
  return `${monotonePath(pts)}L${r2(last[0])},${r2(baseY)}L${r2(pts[0][0])},${r2(baseY)}Z`;
}

/** Minutes between labelled times on the x-axis, per range: 5 to 7 labels each. */
export const TICK_MINUTES: Readonly<Record<number, number>> = {
  5: 1, 15: 3, 30: 5, 60: 10, 120: 20, 360: 60, 720: 120, 1440: 240,
};

/** The labelled times (epoch ms) in [startMs, endMs], on round minutes of the local
 *  clock (`offsetMin` east of UTC). */
export function timeTicks(startMs: number, endMs: number, minutes: number,
                          offsetMin = -new Date(startMs).getTimezoneOffset()): number[] {
  const step = (TICK_MINUTES[minutes] ?? Math.max(1, Math.round(minutes / 6))) * 60_000;
  const off = offsetMin * 60_000;
  const out: number[] = [];
  for (let t = Math.ceil((startMs + off) / step) * step - off; t <= endMs; t += step) out.push(t);
  return out;
}

/** "14:05", or "14:05:30" with seconds, on the local 24-hour clock. */
export function clockLabel(ms: number, withSeconds = false): string {
  return new Date(ms).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
    ...(withSeconds ? { second: '2-digit' as const } : {}),
  });
}

/** A bucket's span for the tooltip: "14:04–14:06". */
export function bucketLabel(startIso: string, bucketSeconds: number): string {
  const start = Date.parse(startIso);
  const seconds = bucketSeconds < 60;
  return `${clockLabel(start, seconds)}–${clockLabel(start + bucketSeconds * 1000, seconds)}`;
}
