/**
 * AuthImage.tsx — images that live behind the Bearer token.
 *
 * WHY THIS EXISTS: `<img src>` cannot carry an Authorization header. Every
 * dataset frame is served by /api/datasets/{id}/frames/{name}, which is gated
 * by the `data_collection` capability, so the browser cannot simply be pointed
 * at the URL — the bytes have to be fetched with the token and handed to the
 * tag as an object URL.
 *
 * AND THE OBJECT URL MUST BE REVOKED. An object URL pins its Blob in memory
 * until it is revoked or the document goes away. A 200-frame grid that never
 * revokes holds 200 decoded JPEGs alive for the life of the tab — tens of
 * megabytes that survive navigating away from the page. Both components below
 * revoke on unmount and whenever the path changes.
 */
import { useEffect, useRef, useState } from 'react';
import { apiBlob } from '@/lib/api';

/** Fetch `path` as an object URL, keeping it alive only while it is displayed. */
function useAuthObjectUrl(path: string | null): { url: string | null; error: string | null } {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Held in a ref as well as state so the cleanup revokes the CURRENT url even
  // when the effect re-runs before React has committed the new state.
  const current = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);

    const release = () => {
      if (current.current) {
        URL.revokeObjectURL(current.current);
        current.current = null;
      }
    };

    if (!path) { release(); setUrl(null); return; }

    apiBlob(path)
      .then(blob => {
        if (cancelled) return;          // unmounted mid-flight: never allocate
        release();
        const next = URL.createObjectURL(blob);
        current.current = next;
        setUrl(next);
      })
      .catch(e => { if (!cancelled) setError(e?.message || 'Could not load image'); });

    return () => { cancelled = true; release(); };
  }, [path]);

  return { url, error };
}

export function AuthImg({ path, alt, className, style, onClick, title }: {
  path: string;
  alt: string;
  className?: string;
  style?: React.CSSProperties;
  /** Supplying this makes the image a lightbox thumbnail. */
  onClick?: () => void;
  title?: string;
}) {
  const { url, error } = useAuthObjectUrl(path);

  if (error) {
    return (
      <div className={className} style={{ ...style, display: 'grid', placeItems: 'center',
        background: 'var(--surface-2, #1b1f27)', color: 'var(--muted, #8b93a1)',
        fontSize: 11, textAlign: 'center', padding: 4 }} title={error}>
        unavailable
      </div>
    );
  }
  if (!url) {
    // Same box as the loaded image so the grid does not reflow as frames arrive.
    return <div className={className} style={{ ...style,
      background: 'var(--surface-2, #1b1f27)' }} aria-busy="true" />;
  }
  return (
    <img
      src={url}
      alt={alt}
      title={title}
      className={className}
      style={{ ...style, cursor: onClick ? 'zoom-in' : undefined }}
      onClick={onClick}
      loading="lazy"
    />
  );
}

export function ImageLightbox({ path, caption, onClose, onPrev, onNext }: {
  path: string | null;
  caption?: string;
  onClose: () => void;
  onPrev?: () => void;
  onNext?: () => void;
}) {
  const { url, error } = useAuthObjectUrl(path);

  useEffect(() => {
    if (!path) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
      if (e.key === 'ArrowLeft') onPrev?.();
      if (e.key === 'ArrowRight') onNext?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [path, onClose, onPrev, onNext]);

  if (!path) return null;
  return (
    <div className="overlay open" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}
         style={{ display: 'grid', placeItems: 'center' }}>
      <div style={{ maxWidth: '92vw', maxHeight: '92vh', display: 'flex',
                    flexDirection: 'column', gap: 8, alignItems: 'center' }}>
        {error
          ? <div style={{ color: 'var(--muted, #8b93a1)' }}>{error}</div>
          : url
            ? <img src={url} alt={caption || 'frame'}
                   style={{ maxWidth: '92vw', maxHeight: '84vh', objectFit: 'contain',
                            borderRadius: 6 }} />
            : <div style={{ color: 'var(--muted, #8b93a1)' }}>Loading…</div>}
        <div style={{ display: 'flex', gap: 12, alignItems: 'center',
                      color: 'var(--muted, #8b93a1)', fontSize: 12 }}>
          {onPrev && <button className="btn btn-sm" onClick={onPrev}>← Prev</button>}
          {caption && <span>{caption}</span>}
          {onNext && <button className="btn btn-sm" onClick={onNext}>Next →</button>}
          <button className="btn btn-sm" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
