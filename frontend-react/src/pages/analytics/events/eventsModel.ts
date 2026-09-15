/**
 * eventsModel.ts — the Events tab's pure logic: ordering, the ticker cap, which
 * rows are new since the last refresh, and time formatting.
 *
 * The API already returns everything newest first. Ordering is still enforced
 * here because the page merges data from more than one response (a refreshed
 * first page over older pages already loaded), and a merge is exactly where an
 * order quietly breaks.
 */
import { TICKER_LIMIT, type AiEvent } from '@/lib/aiEvents';

/** Newest first by when it happened; the id breaks a tie, as the API does. */
export function newestFirst(events: readonly AiEvent[]): AiEvent[] {
  return [...events].sort((a, b) => {
    const dt = Date.parse(b.started_at) - Date.parse(a.started_at);
    if (dt !== 0) return dt;
    return a.id < b.id ? 1 : a.id > b.id ? -1 : 0;
  });
}

/** What the live ticker shows: the newest `limit` events, never more. */
export function tickerItems(latest: readonly AiEvent[], limit = TICKER_LIMIT): AiEvent[] {
  return newestFirst(latest).slice(0, Math.min(limit, TICKER_LIMIT));
}

/**
 * Ids present now that were not on the previous refresh — the rows to animate
 * in. Empty on the first load: a page that flashes every row it opens with is
 * announcing nothing.
 */
export function freshIds(previous: readonly AiEvent[] | null, next: readonly AiEvent[]): Set<string> {
  if (previous == null) return new Set();
  const seen = new Set(previous.map(e => e.id));
  return new Set(next.filter(e => !seen.has(e.id)).map(e => e.id));
}

/** A refreshed newest page over older events already loaded: no duplicates, still ordered. */
export function mergePages(head: readonly AiEvent[], loaded: readonly AiEvent[]): AiEvent[] {
  const byId = new Map<string, AiEvent>();
  for (const e of loaded) byId.set(e.id, e);
  for (const e of head) byId.set(e.id, e);        // the fresher copy wins (an event may have closed)
  return newestFirst([...byId.values()]);
}

export function fmtEventTime(iso: string, now: Date = new Date()): { time: string; date: string | null } {
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const date = d.toDateString() === now.toDateString()
    ? null
    : d.toLocaleDateString([], { day: 'numeric', month: 'short' });
  return { time, date };
}

export function fmtEventDuration(seconds: number | null): string | null {
  if (seconds == null) return null;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
