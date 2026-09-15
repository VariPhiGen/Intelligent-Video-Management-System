/**
 * useActivityCatalog — loads the admin-managed activity-type catalog from the
 * API, with the built-in defaults as an offline fallback. `reload()` re-fetches
 * after the Manage-types modal saves.
 */
import { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { DEFAULT_ACTIVITY_CATALOG, type ActivityMeta } from './activities';

export function useActivityCatalog() {
  const [catalog, setCatalog] = useState<ActivityMeta[]>(DEFAULT_ACTIVITY_CATALOG);

  const reload = useCallback(async () => {
    try {
      const t = await apiFetch<ActivityMeta[]>('/cameras/analytics/types');
      if (Array.isArray(t)) setCatalog(t);
    } catch { /* keep whatever we have (defaults or last good) */ }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  const byKey = Object.fromEntries(catalog.map(a => [a.key, a])) as Record<string, ActivityMeta>;
  return { catalog, byKey, reload };
}
