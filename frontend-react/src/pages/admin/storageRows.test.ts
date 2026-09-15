/**
 * Folding the NVR's per-recording-name storage into camera rows.
 *
 * This is the half of the sub-track storage bug that was always correct, and it
 * is worth pinning precisely because of that: the row total counts the sub's
 * bytes, so the operator selects a row that includes them, is shown a figure
 * that includes them, and is told that figure was freed. The other half — the
 * purge — had to be taught to delete the sub's footage as well (the API's /nvr
 * proxy now fans a whole-camera purge out per track). If this folding were ever
 * dropped, the two halves would silently disagree again in the other direction.
 */
import { describe, expect, it } from 'vitest';
import { bytesOf, foldPerCamera, parentSlug, storageRows } from './storageRows';

const B = (n: number) => ({ total_bytes: n });

describe('parentSlug', () => {
  it('maps a sub recording name to its camera', () => {
    expect(parentSlug('entry-g98a_sub')).toBe('entry-g98a');
  });
  it('leaves a plain slug alone', () => {
    expect(parentSlug('entry-g98a')).toBe('entry-g98a');
  });
  it('only strips a trailing suffix', () => {
    // A slug that merely CONTAINS the suffix is a different camera.
    expect(parentSlug('sub_station-a1b2')).toBe('sub_station-a1b2');
  });
});

describe('bytesOf', () => {
  it('accepts whichever key the NVR build used', () => {
    expect(bytesOf({ total_bytes: 5 })).toBe(5);
    expect(bytesOf({ size_bytes: 5 })).toBe(5);
    expect(bytesOf({ bytes: 5 })).toBe(5);
  });
  it('treats a missing or malformed entry as zero rather than NaN', () => {
    // NaN would propagate into the row total and render as "NaN GB".
    expect(bytesOf({})).toBe(0);
    expect(bytesOf(undefined)).toBe(0);
  });
});

describe('foldPerCamera', () => {
  it('folds a sub into its parent instead of listing it separately', () => {
    const folded = foldPerCamera({ 'entry-g98a': B(100), 'entry-g98a_sub': B(20) });
    expect([...folded.keys()]).toEqual(['entry-g98a']);
  });

  it('counts the sub inside the total the operator is shown', () => {
    const folded = foldPerCamera({ 'entry-g98a': B(100), 'entry-g98a_sub': B(20) });
    expect(folded.get('entry-g98a')).toEqual({ total: 120, sub: 20 });
  });

  it('reports the sub portion separately for the "incl. N low-res" line', () => {
    const folded = foldPerCamera({ 'entry-g98a': B(100), 'entry-g98a_sub': B(20) });
    expect(folded.get('entry-g98a')!.sub).toBe(20);
  });

  it('gives a camera with no sub a zero sub portion', () => {
    const folded = foldPerCamera({ 'gate-a1b2': B(50) });
    expect(folded.get('gate-a1b2')).toEqual({ total: 50, sub: 0 });
  });

  it('still produces a row for a sub whose main has aged out', () => {
    // The sub keeps a much shorter retention, so this is the rarer direction —
    // but dropping the row would hide footage that is genuinely on disk.
    const folded = foldPerCamera({ 'gate-a1b2_sub': B(7) });
    expect(folded.get('gate-a1b2')).toEqual({ total: 7, sub: 7 });
  });

  it('handles an empty or absent payload', () => {
    expect(foldPerCamera({}).size).toBe(0);
    expect(foldPerCamera(null).size).toBe(0);
    expect(foldPerCamera(undefined).size).toBe(0);
  });

  it('keeps two cameras apart', () => {
    const folded = foldPerCamera({
      'a-1111': B(10), 'a-1111_sub': B(1),
      'b-2222': B(20), 'b-2222_sub': B(2),
    });
    expect(folded.get('a-1111')).toEqual({ total: 11, sub: 1 });
    expect(folded.get('b-2222')).toEqual({ total: 22, sub: 2 });
  });
});

describe('storageRows', () => {
  it('sorts biggest first', () => {
    const rows = storageRows(foldPerCamera({
      'small-1111': B(10), 'big-2222': B(90), 'mid-3333': B(50),
    }));
    expect(rows.map(([slug]) => slug)).toEqual(['big-2222', 'mid-3333', 'small-1111']);
  });

  it('ranks on the folded total, so a big sub moves its camera up', () => {
    // The ordering an operator uses to decide what to purge has to reflect what
    // purging that row will actually free.
    const rows = storageRows(foldPerCamera({
      'a-1111': B(60),
      'b-2222': B(50), 'b-2222_sub': B(40),   // 90 folded
    }));
    expect(rows[0][0]).toBe('b-2222');
    expect(rows[0][1]).toBe(90);
  });
});
