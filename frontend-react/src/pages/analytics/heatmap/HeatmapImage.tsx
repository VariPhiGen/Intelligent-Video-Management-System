/**
 * HeatmapImage — one camera's heatmap for TODAY: every stored 5-minute heat
 * block since local midnight plus the one being filled, drawn over a recent
 * camera frame. The same picture the range view shows for "Today, up to now",
 * so a card and the viewer never disagree about what today looks like.
 *
 * Fetched with `apiBlob` into an object URL rather than given to `<img src>`,
 * because the bearer token cannot ride on an image request. Redrawn every
 * minute; the previous URL is released the moment the next one replaces it.
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError, apiBlob } from '@/lib/api';
import {
  RANGE_REFRESH_MS, heatmapFileName, heatmapRangeImagePath, saveObjectUrl, startOfToday,
} from '@/lib/heatmaps';

export function HeatmapImage({ cameraId, name, alt, dimmed }: {
  cameraId: string;
  /** The camera's name, for the downloaded file. */
  name: string;
  alt: string;
  /** A frozen heatmap is shown quietly, so ON and OFF do not look alike. */
  dimmed?: boolean;
}) {
  const [src, setSrc] = useState<string | null>(null);
  const [state, setState] = useState<'loading' | 'ready' | 'empty' | 'failed'>('loading');
  const [tick, setTick] = useState(0);
  const shown = useRef<string | null>(null);
  /** The period the picture on screen covers: today, up to when it was fetched. */
  const period = useRef<{ start: Date; end: Date } | null>(null);

  useEffect(() => {
    const timer = setInterval(() => setTick(t => t + 1), RANGE_REFRESH_MS);
    return () => {
      clearInterval(timer);
      if (shown.current) URL.revokeObjectURL(shown.current);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const now = new Date();
    apiBlob(heatmapRangeImagePath(cameraId, startOfToday(now), now))
      .then(blob => {
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        period.current = { start: startOfToday(now), end: now };
        if (shown.current) URL.revokeObjectURL(shown.current);
        shown.current = url;
        setSrc(url);
        setState('ready');
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setState(e instanceof ApiError && e.status === 404 ? 'empty' : 'failed');
      });
    return () => { cancelled = true; };
  }, [cameraId, tick]);

  if (state === 'empty' || state === 'failed') {
    return (
      <div className="hm-shot hm-shot-empty">
        <span className="glyph" aria-hidden="true">▦</span>
        <p>{state === 'failed' ? 'The heatmap could not be loaded' : 'No heatmap yet'}</p>
      </div>
    );
  }
  return (
    <div className={'hm-shot' + (dimmed ? ' hm-shot-frozen' : '')}>
      {src ? <img src={src} alt={alt} /> : <div className="skel hm-skel" />}
      {src && (
        <button type="button" className="hm-save" title="Save this heatmap, with its background, as a JPEG"
                onClick={e => {
                  e.stopPropagation();                 // the card picks a camera on click
                  if (period.current) {
                    saveObjectUrl(src, heatmapFileName(name, period.current.start, period.current.end));
                  }
                }}>
          ⤓ Download
        </button>
      )}
    </div>
  );
}
