/**
 * useRegistrations.ts — camera-to-plan registration data for the Map tab.
 *
 * Kept beside useMapData's hooks and deliberately separate from them: those
 * load what the map draws at rest, this loads and mutates coverage, which only
 * a camera_manage operator can change and only the coverage layer reads.
 */
import { useCallback, useEffect, useState } from 'react';

import { apiFetch } from '@/lib/api';
import type { CameraRegistration } from '@/lib/types';

import type { Pair } from './homographyFit';

/** Every camera's registration on the current plan — the coverage layer's one
 *  query. A request per camera dot on every map render is the alternative. */
export function usePlanRegistrations(sitemapId: number | null) {
  const [registrations, setRegistrations] = useState<CameraRegistration[]>([]);
  const [loading, setLoading] = useState(false);

  const reload = useCallback(async () => {
    if (sitemapId == null) { setRegistrations([]); return; }
    setLoading(true);
    try {
      setRegistrations(await apiFetch<CameraRegistration[]>(`/sitemaps/${sitemapId}/registrations`));
    } catch {
      // Coverage is an overlay, not the page. A failure here hides footprints
      // and leaves every other layer working, which is better than an error
      // state over a map the operator was using for something else.
      setRegistrations([]);
    } finally {
      setLoading(false);
    }
  }, [sitemapId]);

  useEffect(() => {
    let cancelled = false;
    // The cancelled flag guards against a plan switch mid-flight resolving
    // after the new plan's load and painting the old plan's footprints.
    void (async () => {
      if (sitemapId == null) { setRegistrations([]); return; }
      setLoading(true);
      try {
        const rows = await apiFetch<CameraRegistration[]>(`/sitemaps/${sitemapId}/registrations`);
        if (!cancelled) setRegistrations(rows);
      } catch {
        if (!cancelled) setRegistrations([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [sitemapId]);

  return { registrations, loading, reload };
}

/** Save one registration. Returns the stored row, which carries the
 *  server-computed footprint and error — the client's own numbers are only ever
 *  a preview, and the two disagreeing is a bug worth seeing rather than
 *  hiding. */
export async function saveRegistration(
  cameraId: string,
  body: { sitemap_id: number; preset_token: string; landmark_pairs: Pair[]; homography: number[][] },
): Promise<CameraRegistration> {
  return apiFetch<CameraRegistration>(`/cameras/${cameraId}/registration`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

export async function deleteRegistration(cameraId: string, registrationId: number): Promise<void> {
  await apiFetch(`/cameras/${cameraId}/registration/${registrationId}`, { method: 'DELETE' });
}
