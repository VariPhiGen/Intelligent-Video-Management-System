/**
 * entryExitGeometry — the graph is a smooth line that never lies about a bucket:
 * it passes through every count, never dips below zero or rises above the
 * busiest bucket between two points, and its axes cover what it draws.
 */
import { describe, expect, it } from 'vitest';
import {
  areaPath, bucketLabel, countScale, monotonePath, TICK_MINUTES, timeTicks, type Pt,
} from './entryExitGeometry';
import { ENTRY_EXIT_RANGES } from '@/lib/aiEvents';

/** The cubic segments of a path from monotonePath: [start, c1, c2, end] per segment. */
function segments(path: string): Pt[][] {
  const nums = (path.match(/-?\d+(\.\d+)?/g) || []).map(Number);
  const out: Pt[][] = [];
  let at: Pt = [nums[0], nums[1]];
  for (let i = 2; i + 5 < nums.length; i += 6) {
    const end: Pt = [nums[i + 4], nums[i + 5]];
    out.push([at, [nums[i], nums[i + 1]], [nums[i + 2], nums[i + 3]], end]);
    at = end;
  }
  return out;
}

const cubic = (a: number, b: number, c: number, d: number, t: number) =>
  (1 - t) ** 3 * a + 3 * (1 - t) ** 2 * t * b + 3 * (1 - t) * t ** 2 * c + t ** 3 * d;

describe('countScale', () => {
  it('tops out at or above the busiest bucket, on whole-number ticks from zero', () => {
    for (const max of [0, 1, 4, 5, 9, 12, 23, 41, 100, 257]) {
      const { top, ticks } = countScale(max);
      expect(top).toBeGreaterThanOrEqual(max);
      expect(ticks[0]).toBe(0);
      expect(ticks.at(-1)).toBe(top);
      expect(ticks.every(Number.isInteger)).toBe(true);
      expect(ticks.length).toBeGreaterThanOrEqual(3);
      expect(ticks.length).toBeLessThanOrEqual(6);
    }
  });

  it('gives a quiet graph a readable axis rather than a zero-height one', () => {
    expect(countScale(0)).toEqual({ top: 4, ticks: [0, 1, 2, 3, 4] });
  });
});

describe('monotonePath', () => {
  const counts = [0, 0, 5, 0, 0, 12, 11, 3, 0, 1];
  const baseY = 200;
  const pts: Pt[] = counts.map((c, i) => [i * 40, baseY - c * 10]);

  it('is a smooth curve through every bucket, one cubic segment between each pair', () => {
    const path = monotonePath(pts);
    expect(path.startsWith('M0,200')).toBe(true);
    const segs = segments(path);
    expect(segs).toHaveLength(counts.length - 1);
    segs.forEach((s, i) => {
      expect(s[0]).toEqual(pts[i]);
      expect(s[3]).toEqual(pts[i + 1]);
    });
    expect(path).not.toMatch(/[HV]/);           // no horizontal/vertical runs: not a step graph
  });

  it('never overshoots: between two buckets the line stays within their counts', () => {
    for (const [p0, c1, c2, p3] of segments(monotonePath(pts))) {
      const lo = Math.min(p0[1], p3[1]) - 0.02, hi = Math.max(p0[1], p3[1]) + 0.02;
      for (let t = 0; t <= 1; t += 0.05) {
        const y = cubic(p0[1], c1[1], c2[1], p3[1], t);
        expect(y).toBeGreaterThanOrEqual(lo);
        expect(y).toBeLessThanOrEqual(hi);
      }
    }
  });

  it('keeps a quiet stretch flat on the baseline', () => {
    const [flat] = segments(monotonePath(pts));
    expect(flat.map(p => p[1])).toEqual([200, 200, 200, 200]);
  });

  it('handles no points and one point', () => {
    expect(monotonePath([])).toBe('');
    expect(monotonePath([[5, 7]])).toBe('M5,7');
  });
});

describe('areaPath', () => {
  it('is the line closed down to the baseline', () => {
    const pts: Pt[] = [[10, 50], [20, 30], [30, 60]];
    const area = areaPath(pts, 100);
    expect(area.startsWith(monotonePath(pts))).toBe(true);
    expect(area.endsWith('L30,100L10,100Z')).toBe(true);
  });
});

describe('timeTicks', () => {
  const start = Date.UTC(2026, 8, 15, 9, 0, 0);

  it('labels every range with 5 to 7 evenly spaced times inside the window', () => {
    for (const { minutes } of ENTRY_EXIT_RANGES) {
      const end = start + minutes * 60_000;
      const ticks = timeTicks(start, end, minutes, 0);
      expect(ticks.length).toBeGreaterThanOrEqual(5);
      expect(ticks.length).toBeLessThanOrEqual(7);
      expect(ticks[0]).toBeGreaterThanOrEqual(start);
      expect(ticks.at(-1)!).toBeLessThanOrEqual(end);
      const steps = new Set(ticks.slice(1).map((t, i) => t - ticks[i]));
      expect([...steps]).toEqual([TICK_MINUTES[minutes] * 60_000]);
    }
  });

  it('lands on round times of the local clock, whatever its offset from UTC', () => {
    // UTC+5:30: the hour marks of the local clock are at :30 UTC.
    const ticks = timeTicks(start, start + 1440 * 60_000, 1440, 330);
    for (const t of ticks) expect((t + 330 * 60_000) % (240 * 60_000)).toBe(0);
  });
});

describe('bucketLabel', () => {
  it('names the bucket span, with seconds only when the buckets are shorter than a minute', () => {
    expect(bucketLabel('2026-09-15T09:04:00Z', 120)).toMatch(/^\d{2}:\d{2}–\d{2}:\d{2}$/);
    expect(bucketLabel('2026-09-15T09:04:10Z', 10)).toMatch(/^\d{2}:\d{2}:\d{2}–\d{2}:\d{2}:\d{2}$/);
  });
});
