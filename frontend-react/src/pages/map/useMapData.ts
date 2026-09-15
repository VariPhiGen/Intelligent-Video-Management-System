/**
 * useMapData.ts — the Map tab's data loading, one hook per resource.
 *
 * These were four separate effects buried in MapPage. Pulled out because each
 * is a self-contained fetch-and-cache with its own cleanup, and the object-URL
 * hygiene (revoke on unmount AND on a stale resolution) is easy to get wrong
 * when it sits next to unrelated state.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { apiBlob, apiFetch } from '@/lib/api';
import type { SitemapMeta } from '@/lib/types';

import type { MotionEvent } from './MockLayers';

/** The sitemap list plus which one is showing. `sitemaps === null` means the
 *  first load hasn't finished — the page renders a spinner for that. */
export function useSitemaps() {
  const [sitemaps, setSitemaps] = useState<SitemapMeta[] | null>(null);
  const [sitemapErr, setSitemapErr] = useState('');
  const [currentId, setCurrentId] = useState<number | null>(null);

  const loadSitemaps = useCallback(async (selectId?: number) => {
    try {
      const list = await apiFetch<SitemapMeta[]>('/sitemaps');
      setSitemaps(list);
      setSitemapErr('');
      if (selectId != null) {
        setCurrentId(selectId);
      } else {
        setCurrentId(cur => (cur != null && list.some(s => s.id === cur)) ? cur : (list[0]?.id ?? null));
      }
    } catch (e: any) {
      setSitemaps([]);
      setSitemapErr(e.message);
    }
  }, []);

  useEffect(() => { void loadSitemaps(); }, [loadSitemaps]);

  const currentMap = useMemo(
    () => sitemaps?.find(s => s.id === currentId) || null,
    [sitemaps, currentId],
  );

  return { sitemaps, sitemapErr, currentId, setCurrentId, currentMap, loadSitemaps };
}

/** The plan image as an object URL. Cancelled-flag pattern: revoke on cleanup
 *  AND on a stale resolution (map switched mid-fetch), so map A's image can't
 *  keep rendering under map B's dots. */
export function useSitemapImage(currentId: number | null) {
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [imgLoading, setImgLoading] = useState(false);
  const [imgError, setImgError] = useState(false);

  useEffect(() => {
    // Clear synchronously on every map switch — the previous effect run's
    // cleanup revokes the old object URL.
    setImgUrl(null);
    if (currentId == null) return;
    let cancelled = false;
    let url: string | null = null;
    setImgLoading(true);
    setImgError(false);
    apiBlob(`/sitemaps/${currentId}/image`).then(b => {
      const created = URL.createObjectURL(b);
      if (cancelled) { URL.revokeObjectURL(created); return; }
      url = created;
      setImgUrl(created);
      setImgLoading(false);
    }).catch(() => {
      if (!cancelled) { setImgError(true); setImgLoading(false); }
    });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [currentId]);

  return { imgUrl, imgLoading, imgError };
}

/** Motion events for the heatmap. Fetched once per map switch (not per view
 *  switch) so flipping to Heatmap and back doesn't re-fetch. */
export function useMotionEvents(currentId: number | null) {
  const [motionEvents, setMotionEvents] = useState<MotionEvent[]>([]);

  useEffect(() => {
    if (currentId == null) { setMotionEvents([]); return; }
    let cancelled = false;
    apiFetch<{ events: MotionEvent[] }>('/motion/events?limit=1000')
      .then(r => { if (!cancelled) setMotionEvents(r.events || []); })
      .catch(() => { if (!cancelled) setMotionEvents([]); });
    return () => { cancelled = true; };
  }, [currentId]);

  return motionEvents;
}

/** Snapshot for the detail panel — same object-URL hygiene, keyed on the
 *  selected camera so a mid-flight deselect/reselect can't clobber it. */
export function useCameraSnapshot(selectedId: string | null) {
  const [snapUrl, setSnapUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedId) { setSnapUrl(null); return; }
    let cancelled = false;
    let url: string | null = null;
    setSnapUrl(null);
    apiBlob(`/cameras/${selectedId}/snapshot`).then(b => {
      const created = URL.createObjectURL(b);
      if (cancelled) { URL.revokeObjectURL(created); return; }
      url = created;
      setSnapUrl(created);
    }).catch(() => { /* degrade gracefully — hide the image */ });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [selectedId]);

  return snapUrl;
}
