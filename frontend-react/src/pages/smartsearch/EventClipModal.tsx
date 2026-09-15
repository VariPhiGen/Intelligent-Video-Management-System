/**
 * EventClipModal.tsx — plays a short clip of recorded footage in a popup,
 * pulled from the NVR /clip endpoint (fragmented MP4) through the
 * authenticated proxy.
 *
 * Smart Search opens it on a hit: ~20s centred on the moment (the 10s/10s
 * defaults). The AI Events page opens it on an activity event, with a window
 * that covers the event — its pre-roll, its duration and its post-roll.
 *
 * Additive and read-only: nothing here changes search, indexing, events or
 * Playback.
 */
import { useEffect, useState, type CSSProperties } from 'react';

import { apiBlob } from '@/lib/api';
import { dtLocal } from '@/lib/format';
import type { SearchCamera } from '@/lib/smartsearch';

export function EventClipModal({ cam, whenMs, before = 10, after = 10, label, onClose }: {
  cam: Pick<SearchCamera, 'slug' | 'name'>;
  /** The moment the clip is anchored on. */
  whenMs: number;
  /** Seconds of footage before and after `whenMs`. */
  before?: number;
  after?: number;
  /** What happened, shown beside the camera name (e.g. the activity). */
  label?: string;
  onClose: () => void;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    const ts = new Date(whenMs).toISOString();
    // apiBlob prepends the '/api' base, so the path here is '/nvr/clip'
    // (the proxy is mounted at /api/nvr) — NOT '/api/nvr/clip', which would
    // double the prefix to /api/api/nvr/clip and 404.
    apiBlob(
      `/nvr/clip?camera=${encodeURIComponent(cam.slug)}` +
        `&timestamp=${encodeURIComponent(ts)}&before=${before}&after=${after}`,
    )
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setErr('No recording available for this moment.');
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [cam.slug, whenMs, before, after]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const backdrop: CSSProperties = {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)', zIndex: 1000,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  };
  const boxStyle: CSSProperties = {
    background: '#111', borderRadius: 8, padding: 12, width: 720, maxWidth: '92vw',
    boxShadow: '0 12px 48px rgba(0,0,0,0.55)',
  };
  const head: CSSProperties = {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    gap: 12, color: '#ddd', fontSize: 13, marginBottom: 8,
  };
  const message: CSSProperties = { color: '#bbb', textAlign: 'center', padding: '56px 0' };
  const videoStyle: CSSProperties = {
    width: '100%', maxHeight: '70vh', background: '#000', borderRadius: 4,
  };

  return (
    <div style={backdrop} onClick={onClose}>
      <div style={boxStyle} onClick={(e) => e.stopPropagation()}
           role="dialog" aria-modal="true" aria-label="Event clip">
        <div style={head}>
          <span>{cam.name || cam.slug}{label ? ` · ${label}` : ''} · {dtLocal(new Date(whenMs))}</span>
          <button className="btn-secondary btn-sm" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {err
          ? <div style={message}>{err}</div>
          : src
            ? <video src={src} controls autoPlay style={videoStyle} />
            : <div style={message}>Preparing clip…</div>}
      </div>
    </div>
  );
}

