/**
 * tripwire.ts — what a tripwire's stored direction means.
 *
 * A tripwire is a gateway line. Its two stored points, A and B, are only the
 * line's ends; nobody walks from A to B. `direction` names which way ACROSS the
 * line people walk when they ENTER, and crossing the other way is an exit:
 *
 *   a2b   entry heads to the left of A→B as drawn on the frame
 *   b2a   entry heads to the right of A→B
 *   both  no entry direction set — Entry / Exit does not count this line
 *
 * The analytics engine reads it the same way
 * (services/analytics/analytics/activities/entry_exit_WLE_logs.py).
 */
import type { AnalyticsRegion } from './types';

export type TripwireDirection = NonNullable<AnalyticsRegion['direction']>;

/** Does this direction say which way is Entry? */
export function hasEntryDirection(direction: AnalyticsRegion['direction']): direction is 'a2b' | 'b2a' {
  return direction === 'a2b' || direction === 'b2a';
}

/** The way people walk to enter, as a unit vector in screen pixels (y down), or
 *  null when the tripwire has no entry direction. `width`/`height` are the
 *  frame's pixel size, so the arrow is perpendicular on the frame as shown. */
export function entryVector(
  points: number[][], direction: AnalyticsRegion['direction'], width = 16, height = 9,
): [number, number] | null {
  if (!hasEntryDirection(direction) || points.length < 2) return null;
  const dx = (points[1][0] - points[0][0]) * width;
  const dy = (points[1][1] - points[0][1]) * height;
  const len = Math.hypot(dx, dy);
  if (!len) return null;
  const s = direction === 'a2b' ? 1 : -1;
  return [(s * dy) / len, (s * -dx) / len];
}

const ARROWS = ['→', '↘', '↓', '↙', '←', '↖', '↑', '↗'];

/** The entry direction as one arrow character, as it points on the frame. */
export function entryArrow(
  points: number[][], direction: AnalyticsRegion['direction'], width = 16, height = 9,
): string | null {
  const v = entryVector(points, direction, width, height);
  if (!v) return null;
  const step = Math.round(Math.atan2(v[1], v[0]) / (Math.PI / 4));
  return ARROWS[((step % 8) + 8) % 8];
}
