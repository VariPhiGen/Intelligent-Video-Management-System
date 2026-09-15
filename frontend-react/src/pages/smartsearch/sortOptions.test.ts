/** The ORDER control follows the SORT BY mode.
 *
 *  Two modes share one direction on the wire and name it differently, so the
 *  wrong vocabulary would offer "Newest" while sorting by similarity — the
 *  control would look broken while behaving correctly.
 */
import { describe, it, expect } from 'vitest';
import { ORDER_OPTIONS, orderHint, orderOptions } from './sortOptions';
import type { SortBy } from '@/lib/smartsearch';

describe('orderOptions', () => {
  it('offers time words when sorting by time', () => {
    expect(orderOptions('time')).toEqual([
      ['desc', 'Newest → Oldest'],
      ['asc', 'Oldest → Newest'],
    ]);
  });

  it('offers similarity words when sorting by confidence', () => {
    expect(orderOptions('confidence')).toEqual([
      ['desc', 'Highest → Lowest'],
      ['asc', 'Lowest → Highest'],
    ]);
  });

  it('switching the mode swaps the whole vocabulary', () => {
    const labels = (by: SortBy) => orderOptions(by).map(([, label]) => label);
    expect(labels('time')).not.toEqual(labels('confidence'));
  });

  it('keeps the direction values identical across modes', () => {
    // The wire carries desc/asc; only the words change. That is what lets a
    // mode switch keep the direction the operator already chose.
    const dirs = (by: SortBy) => orderOptions(by).map(([v]) => v);
    expect(dirs('time')).toEqual(['desc', 'asc']);
    expect(dirs('confidence')).toEqual(['desc', 'asc']);
  });

  it('lists the default direction first in both modes', () => {
    expect(orderOptions('time')[0][0]).toBe('desc');
    expect(orderOptions('confidence')[0][0]).toBe('desc');
  });

  it('falls back to the time vocabulary for a mode it does not know', () => {
    // A form that renders beats one that throws on a stale query string.
    expect(orderOptions('nonsense' as SortBy)).toEqual(ORDER_OPTIONS.time);
  });

  it('explains the active field, and only that field', () => {
    expect(orderHint('time')).toMatch(/when it happened/);
    expect(orderHint('time')).toMatch(/30 days/);
    expect(orderHint('confidence')).toMatch(/match/);
    expect(orderHint('confidence')).not.toMatch(/30 days/);
  });
});
