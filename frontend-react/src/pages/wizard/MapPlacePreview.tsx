/**
 * MapPlacePreview.tsx — click-to-place preview of a site plan, used while
 * assigning a newly discovered camera.
 *
 * Object-URL hygiene matters here: the plan is fetched as a blob, so a stale
 * resolution (the sitemap changed mid-fetch) must revoke rather than render.
 */
import { useEffect, useState } from 'react';

import { apiBlob } from '@/lib/api';

export function MapPlacePreview({ sitemapId, x, y, onPlace }: {
  sitemapId: string; x: number | null; y: number | null;
  onPlace: (x: number, y: number) => void;
}) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    let url: string | null = null;
    apiBlob(`/sitemaps/${sitemapId}/image`).then(b => {
      const created = URL.createObjectURL(b);
      // A fetch that outlives its effect run (sitemap switched / unmounted
      // mid-flight) must neither clobber the newer image nor leak its URL.
      if (cancelled) { URL.revokeObjectURL(created); return; }
      url = created;
      setSrc(created);
    }).catch(() => { if (!cancelled) setSrc(null); });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [sitemapId]);
  if (!src) return null;
  return (
    <div style={{ position: 'relative', maxWidth: 420, cursor: 'crosshair' }}
      onClick={e => {
        const r = e.currentTarget.getBoundingClientRect();
        onPlace(
          Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1),
          Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1),
        );
      }}>
      <img src={src} style={{ width: '100%', display: 'block', borderRadius: 6 }} />
      {x != null && y != null && (
        <span style={{
          position: 'absolute', left: `${x * 100}%`, top: `${y * 100}%`,
          transform: 'translate(-50%, -50%)', width: 12, height: 12,
          borderRadius: '50%', background: 'var(--accent)',
          boxShadow: '0 0 0 3px color-mix(in srgb, var(--accent) 35%, transparent)',
        }} />
      )}
    </div>
  );
}

// ── Full devices table (legacy "Discovered devices" card, step 3 only) ───────
