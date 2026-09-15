/**
 * useEventsOverview — the Events tab's live feed: the ticker and camera cards,
 * refreshed on a short poll.
 *
 * Polling rather than a push channel because every other live surface in this
 * SPA polls through the one `fetch` seam (see src/test/api.ts on why). The poll
 * pauses while the tab is hidden and is chained with setTimeout, not
 * setInterval, so a slow response can never stack a second request behind it.
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '@/lib/api';
import { fetchEventsOverview, type AiEvent, type EventsOverview } from '@/lib/aiEvents';
import { freshIds } from './eventsModel';

export const OVERVIEW_POLL_MS = 5000;

export type OverviewError = { kind: 'denied' } | { kind: 'unreachable'; message: string };

function allEvents(d: EventsOverview): AiEvent[] {
  return [...d.latest, ...d.cameras.flatMap(c => c.events)];
}

export function useEventsOverview(pollMs = OVERVIEW_POLL_MS) {
  const [data, setData] = useState<EventsOverview | null>(null);
  const [error, setError] = useState<OverviewError | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  const previous = useRef<AiEvent[] | null>(null);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => {
      if (alive) timer = setTimeout(load, pollMs);
    };

    async function load() {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        schedule();
        return;
      }
      try {
        const d = await fetchEventsOverview();
        if (!alive) return;
        const events = allEvents(d);
        setFresh(freshIds(previous.current, events));
        previous.current = events;
        setData(d);
        setError(null);
      } catch (e: unknown) {
        if (!alive) return;
        setError(e instanceof ApiError && e.status === 403
          ? { kind: 'denied' }
          : { kind: 'unreachable', message: e instanceof Error ? e.message : String(e) });
        // A denied role will stay denied; do not keep asking.
        if (e instanceof ApiError && e.status === 403) return;
      }
      schedule();
    }

    load();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [pollMs]);

  return { data, error, fresh };
}
