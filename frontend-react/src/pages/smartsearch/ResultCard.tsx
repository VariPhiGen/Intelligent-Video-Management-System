/**
 * ResultCard.tsx — one search hit: the frame it came from, with the matched
 * object boxed, and the details under it.
 *
 * THE IMAGE IS THE WHOLE FRAME, not the crop. A crop answers "what did the
 * index match"; the operator's next question is "where was this and what was
 * going on around them", which a person-shaped cut-out cannot answer. The frame
 * carries both, and the box says which object in it this result is about.
 *
 * THE BOX CARRIES NO LABEL. The tracker id, the score and the rest sit in the
 * details below. Printing an id over the picture only covers the thing the
 * operator is trying to look at.
 *
 * FALLING BACK TO THE CROP. Whole frames exist for rows indexed since the
 * feature landed, and for SEARCH_FRAME_RETENTION_DAYS (7 days) after that. An
 * older hit has none and shows its crop, unboxed — a crop already IS the box.
 *
 * Always playable: every hit that reaches the page resolves to a camera this
 * VMS records, so the action never dead-ends (see SmartSearchPage's note).
 */
import { useEffect, useState, type CSSProperties } from 'react';

import { apiBlob } from '@/lib/api';
import { dtLocal, timeAgo } from '@/lib/format';
import type { SearchCamera } from '@/lib/smartsearch';

import { boxStyle } from './trackerId';
import { Score } from './searchUi';

export interface Shot {
  domain: string;
  id: string;
  /** The matched detection, normalised 0-1 of the source frame. */
  bbox?: number[] | null;
  /** Whether the index kept the whole frame for this row. */
  hasFrame?: boolean | null;
}

/** The frame this hit came from, with the matched object boxed; the crop when
 *  there is no frame. Both are fetched through the authenticated proxies
 *  (apiBlob → object URL), so neither depends on the live recording. */
export function ResultShot({ domain, id, bbox, hasFrame }: Shot) {
  const [src, setSrc] = useState<string | null>(null);
  const [framed, setFramed] = useState(false);
  const [failed, setFailed] = useState(false);
  const [ratio, setRatio] = useState<number | null>(null);

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    const q = `domain=${encodeURIComponent(domain)}&id=${encodeURIComponent(id)}`;
    const crop = () => apiBlob(`/search/image?${q}`).then(b => ({ b, framed: false }));
    // A frame 404 is ordinary — frames age out days before rows do — so it
    // falls back to the crop rather than to "no image".
    const load = hasFrame
      ? apiBlob(`/search/frame?${q}`).then(b => ({ b, framed: true })).catch(crop)
      : crop();
    load
      .then(({ b, framed: gotFrame }) => {
        if (cancelled) return;
        url = URL.createObjectURL(b);
        setSrc(url);
        setFramed(gotFrame);
      })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [domain, id, hasFrame]);

  // No image at all: older rows, or an index without stored crops. The card is
  // still worth showing for its metadata, so this takes no space.
  if (failed) return null;

  // THE BOX MUST BE THE IMAGE'S BOX. The wrapper takes the frame's own aspect
  // ratio once it has loaded and the image fills it exactly, so a percentage
  // inside the wrapper is a percentage of the picture. The stored frame is the
  // source frame downscaled with its aspect kept, and the bbox is normalised
  // against that same frame, so no resolution arithmetic is needed here.
  const box = framed ? boxStyle(bbox) : null;
  return (
    <div className={`ss-shot${framed ? ' framed' : ''}`}
         style={framed && ratio ? { aspectRatio: String(ratio) } : undefined}>
      <img
        src={src ?? undefined}
        alt={src ? domain : ''}
        className={src ? 'ready' : undefined}
        onLoad={e => {
          const i = e.currentTarget;
          if (i.naturalWidth && i.naturalHeight) setRatio(i.naturalWidth / i.naturalHeight);
        }}
      />
      {box && <span className="ss-shot-box" style={box} />}
    </div>
  );
}

/** The stored crop alone — no frame, no box — for a result whose picture IS the
 *  crop. The Faces tab uses it: a face row stores the aligned 112x112 face SFace
 *  embedded and keeps no frame, so ResultShot could only ever fall back to this.
 *  Fetched through the authenticated /api/search/image proxy (apiBlob → object
 *  URL), because an <img src> cannot carry the bearer token. Hidden if no crop
 *  image exists. */
export function CropThumb({ domain, id, className, height = 150 }: {
  domain: string; id: string; className?: string; height?: number | string;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    apiBlob(`/search/image?domain=${encodeURIComponent(domain)}&id=${encodeURIComponent(id)}`)
      .then((blob) => { if (!cancelled) { url = URL.createObjectURL(blob); setSrc(url); } })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; if (url) URL.revokeObjectURL(url); };
  }, [domain, id]);
  if (failed) return null;
  // A caller that brings its own class brings its own geometry with it; the
  // inline defaults stay for a caller that brings none.
  const style: CSSProperties = className ? {} : {
    width: '100%', height, objectFit: 'cover', borderRadius: 6,
    background: '#0c0c0c', marginBottom: 8, display: 'block',
  };
  return src
    ? <img src={src} alt={domain} style={style} className={className} />
    : <div style={style} className={className} aria-label="loading crop" />;
}

export function ResultCard({
  title, subtitle, when, sensor, camera, score, facts, onOpen, onShowEvent, shot,
}: {
  title: string;
  subtitle?: string | null;
  when: number;
  sensor: string | null;
  camera: SearchCamera;
  score: number;
  facts: Array<[string, string | null | undefined]>;
  onOpen: () => void;
  onShowEvent: () => void;
  shot?: Shot;
}) {
  const shown = facts.filter(([, v]) => v !== null && v !== undefined && v !== '');
  return (
    <article className="ss-card">
      {shot && <ResultShot {...shot} />}
      <div className="ss-card-hd">
        <div className="ss-card-title">
          {title}
          {subtitle && <span className="ss-card-sub">{subtitle}</span>}
        </div>
        <Score value={score} />
      </div>

      <div className="ss-card-meta">
        <span
          className="ss-cam"
          title={sensor && sensor !== camera.slug ? `Indexed as “${sensor}”` : camera.slug}
        >
          {camera.name || camera.slug}
        </span>
        <span className="ss-when" title={dtLocal(new Date(when))}>
          {timeAgo(new Date(when))}
        </span>
      </div>

      {shown.length > 0 && (
        <dl className="ss-facts">
          {shown.map(([k, v]) => (
            <div key={k} className="ss-fact">
              <dt>{k}</dt><dd>{v}</dd>
            </div>
          ))}
        </dl>
      )}

      <div className="ss-card-foot">
        {/* "Open in Playback" hidden per updated UI — re-enable to jump to this
            moment on the full Playback timeline (onOpen).
        <button className="btn-secondary btn-sm" onClick={onOpen} title="Open this moment in Playback">
          ⏱ Open in Playback
        </button>
        */}
        <button className="btn-secondary btn-sm" onClick={onShowEvent} title="Play ~20s of this event in a popup">
          ⏱ Open Playback
        </button>
      </div>
    </article>
  );
}
