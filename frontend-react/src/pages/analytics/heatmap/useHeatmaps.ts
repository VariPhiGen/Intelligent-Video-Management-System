/**
 * useHeatmaps — the Heatmap page's one read, on a poll.
 *
 * The page DOES NOT drive the analytics. Heat is accumulated and rendered by
 * the analytics service on its own schedule whether or not anybody is looking;
 * this hook only asks the API for the latest artifact metadata, on the same
 * cadence the backend renders at. Opening the page starts nothing and closing
 * it stops nothing.
 *
 * Same shape as useEventsOverview: a chained setTimeout (never setInterval, so
 * a slow response cannot stack requests), paused while the tab is hidden, and a
 * 403 that stops asking — a denied capability will still be denied in a minute.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '@/lib/api';
import { fetchHeatmaps, type HeatmapOverview } from '@/lib/heatmaps';

/** The backend renders about once a minute; asking faster only costs requests. */
export const HEATMAP_POLL_MS = 60_000;

export type HeatmapError = { kind: 'denied' } | { kind: 'unreachable'; message: string };

export function useHeatmaps(pollMs = HEATMAP_POLL_MS) {
  const [data, setData] = useState<HeatmapOverview | null>(null);
  const [error, setError] = useState<HeatmapError | null>(null);
  const [loading, setLoading] = useState(true);
  const [nudge, setNudge] = useState(0);
  const denied = useRef(false);

  /** Read again now — after switching a camera, so the card is not a minute stale. */
  const refresh = useCallback(() => setNudge(n => n + 1), []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const schedule = () => { if (alive && !denied.current) timer = setTimeout(load, pollMs); };

    async function load() {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        schedule();
        return;
      }
      try {
        const got = await fetchHeatmaps();
        if (!alive) return;
        setData(got);
        setError(null);
      } catch (e: unknown) {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 403) {
          denied.current = true;
          setError({ kind: 'denied' });
          setLoading(false);
          return;                       // a denied role stays denied
        }
        setError({ kind: 'unreachable',
                   message: e instanceof Error ? e.message : String(e) });
      }
      if (alive) setLoading(false);
      schedule();
    }

    load();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [pollMs, nudge]);

  return { data, error, loading, refresh };
}
