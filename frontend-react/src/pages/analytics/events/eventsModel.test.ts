/**
 * eventsModel — newest-first ordering, the ten-event ticker cap, and merging.
 */
import { describe, expect, it } from 'vitest';
import { aiEvent } from '@/test/aiEvents';
import { fmtEventDuration, freshIds, mergePages, newestFirst, tickerItems } from './eventsModel';

const at = (min: number) => new Date(Date.UTC(2026, 8, 13, 14, min)).toISOString();

describe('newestFirst', () => {
  it('orders by when the event happened, newest first', () => {
    const out = newestFirst([aiEvent('a', at(1)), aiEvent('b', at(9)), aiEvent('c', at(5))]);
    expect(out.map(e => e.id)).toEqual(['b', 'c', 'a']);
  });

  it('breaks a tie on id the same way the API does', () => {
    const out = newestFirst([aiEvent('1', at(3)), aiEvent('3', at(3)), aiEvent('2', at(3))]);
    expect(out.map(e => e.id)).toEqual(['3', '2', '1']);
  });

  it('does not reorder the caller\'s array', () => {
    const input = [aiEvent('a', at(1)), aiEvent('b', at(2))];
    newestFirst(input);
    expect(input.map(e => e.id)).toEqual(['a', 'b']);
  });
});

describe('tickerItems', () => {
  const twelve = Array.from({ length: 12 }, (_, i) => aiEvent(`e${i}`, at(i)));

  it('never shows more than ten', () => {
    expect(tickerItems(twelve)).toHaveLength(10);
    expect(tickerItems(twelve, 50)).toHaveLength(10);
  });

  it('keeps the ten NEWEST, whatever order they arrived in', () => {
    const ids = tickerItems([...twelve].reverse()).map(e => e.id);
    expect(ids).toEqual(['e11', 'e10', 'e9', 'e8', 'e7', 'e6', 'e5', 'e4', 'e3', 'e2']);
  });
});

describe('freshIds', () => {
  it('marks nothing on the first load', () => {
    expect(freshIds(null, [aiEvent('a', at(1))]).size).toBe(0);
  });

  it('marks exactly the events that were not there last time', () => {
    const before = [aiEvent('a', at(1)), aiEvent('b', at(2))];
    const after = [aiEvent('c', at(3)), ...before];
    expect([...freshIds(before, after)]).toEqual(['c']);
  });
});

describe('mergePages', () => {
  it('lays a refreshed first page over loaded pages without duplicates, still newest first', () => {
    const loaded = [aiEvent('b', at(5)), aiEvent('a', at(1))];
    const head = [aiEvent('c', at(8)), aiEvent('b', at(5), { ended_at: at(6), duration_s: 60 })];
    const out = mergePages(head, loaded);
    expect(out.map(e => e.id)).toEqual(['c', 'b', 'a']);
    expect(out[1].duration_s).toBe(60);           // the fresher copy of `b` won
  });
});

describe('fmtEventDuration', () => {
  it('reads short and long intervals', () => {
    expect(fmtEventDuration(null)).toBeNull();
    expect(fmtEventDuration(42)).toBe('42s');
    expect(fmtEventDuration(125)).toBe('2m 5s');
    expect(fmtEventDuration(3 * 3600 + 7 * 60)).toBe('3h 7m');
  });
});
