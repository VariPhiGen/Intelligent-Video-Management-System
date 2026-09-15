/**
 * tripwire — the entry arrow points ACROSS the line, never along it, and the
 * opposite setting points the opposite way.
 */
import { describe, expect, it } from 'vitest';
import { entryArrow, entryVector, hasEntryDirection } from './tripwire';

const LEFT_TO_RIGHT = [[0.2, 0.5], [0.8, 0.5]];
const TOP_TO_BOTTOM = [[0.5, 0.2], [0.5, 0.8]];

describe('tripwire entry direction', () => {
  it('points across a horizontal line: A→B is up the frame, B→A down', () => {
    expect(entryArrow(LEFT_TO_RIGHT, 'a2b')).toBe('↑');
    expect(entryArrow(LEFT_TO_RIGHT, 'b2a')).toBe('↓');
  });

  it('points across a vertical line: A→B is right, B→A left', () => {
    expect(entryArrow(TOP_TO_BOTTOM, 'a2b')).toBe('→');
    expect(entryArrow(TOP_TO_BOTTOM, 'b2a')).toBe('←');
  });

  it('is perpendicular to the line on the frame, whatever its aspect', () => {
    const line = [[0.1, 0.2], [0.7, 0.9]];
    const v = entryVector(line, 'a2b', 1280, 720)!;
    const along = [(line[1][0] - line[0][0]) * 1280, (line[1][1] - line[0][1]) * 720];
    expect(Math.abs(v[0] * along[0] + v[1] * along[1])).toBeLessThan(1e-9);
    expect(Math.hypot(v[0], v[1])).toBeCloseTo(1);
    const w = entryVector(line, 'b2a', 1280, 720)!;
    expect([w[0], w[1]]).toEqual([-v[0], -v[1]]);
  });

  it('has no arrow when no entry direction is set', () => {
    expect(entryArrow(LEFT_TO_RIGHT, 'both')).toBeNull();
    expect(entryArrow(LEFT_TO_RIGHT, undefined)).toBeNull();
    expect(entryVector([[0.5, 0.5], [0.5, 0.5]], 'a2b')).toBeNull();
    expect([hasEntryDirection('a2b'), hasEntryDirection('b2a'), hasEntryDirection('both')]).toEqual([true, true, false]);
  });
});
