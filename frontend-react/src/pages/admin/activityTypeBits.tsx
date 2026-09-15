/**
 * activityTypeBits.tsx — helpers behind the Activity types page.
 *
 * The page used to author activity types and their settings, and this module
 * held that editor's primitives. Activities and their settings now come from
 * the analytics engine's registry, so only what the read-only page needs is
 * left.
 */
import type { Camera } from '@/lib/types';

/** {typeKey: number of cameras with at least one activity of that type}. */
export function countUsage(cameras: Camera[]): Record<string, number> {
  const out: Record<string, number> = {};
  cameras.forEach(c => {
    const types = new Set((c.analytics_config?.activities || []).map(a => a.type));
    types.forEach(t => { out[t] = (out[t] || 0) + 1; });
  });
  return out;
}
