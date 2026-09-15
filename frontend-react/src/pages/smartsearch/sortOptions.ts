/**
 * sortOptions.ts — the ORDER choices that belong to each SORT BY mode.
 *
 * The two modes share one direction on the wire ("desc" / "asc") and describe
 * it in their own words: newest and highest are the same direction, said of
 * different fields. Keeping the mapping here, away from the markup, is what
 * lets it be tested without mounting the form.
 */
import type { SortBy, SortDir } from '@/lib/smartsearch';

export const ORDER_OPTIONS: Record<SortBy, ReadonlyArray<readonly [SortDir, string]>> = {
  time: [['desc', 'Newest → Oldest'], ['asc', 'Oldest → Newest']],
  confidence: [['desc', 'Highest → Lowest'], ['asc', 'Lowest → Highest']],
};

/** The ORDER options for a mode. An unknown mode reads as time, which is the
 *  default everywhere else too — a form that renders beats one that throws. */
export function orderOptions(by: SortBy): ReadonlyArray<readonly [SortDir, string]> {
  return ORDER_OPTIONS[by] ?? ORDER_OPTIONS.time;
}

/** What the ORDER control means in this mode, for its tooltip. */
export function orderHint(by: SortBy): string {
  return by === 'time'
    ? 'Orders by when it happened, and nothing else. Oldest first reaches back at most 30 days, unless From/To says otherwise.'
    : 'Orders by how well each result matches the words you typed, and nothing else.';
}
