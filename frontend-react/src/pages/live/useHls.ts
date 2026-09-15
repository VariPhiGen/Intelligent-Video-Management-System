/**
 * useHls.ts — one HLS attachment per <video>, ported from the legacy
 * liveAttach(): low-latency config, offline flag on fatal errors, 15 s retry
 * loop while mounted, full teardown (destroy + clear timers) on unmount.
 *
 * Falls back to the camera's low-resolution sub stream when the main will not
 * play. That is not an optimisation, it is the difference between a picture and
 * a black tile: measured on this appliance, Chrome reports
 * `MediaSource.isTypeSupported('…hvc1…') === false`, and every HEVC camera's
 * live tile sat at `readyState: 0` with no error — five of seven cameras
 * showing nothing at all, silently. Where the sub is H.264 (this fleet has
 * several) attaching to it makes the camera visible.
 *
 * The fallback is reactive rather than predictive on purpose: the frontend does
 * not know the main stream's codec, and "the main failed to attach" is a more
 * reliable signal than any guess from camera metadata. It also covers reasons
 * that have nothing to do with codecs — a main path the relay has dropped, say.
 *
 * `hasSub` is "a sub stream EXISTS" (`sub_track.url_raw`), not "the sub is
 * being recorded". Those are different questions and keying this on the
 * recording flag made the whole fallback inert: the relay only created a
 * `<slug>_sub` path for a sub that was switched on for RECORDING, so on every
 * camera whose sub was merely discovered there was nothing to attach to. An
 * H.265 camera with a perfectly good H.264 sub still showed "Signal lost".
 * The relay now carries a resolved sub on demand — see
 * services/tracks.py::relay_tracks_for.
 */
import { useEffect, useRef, useState } from 'react';
import Hls from 'hls.js';
import { hlsSrc } from '@/lib/api';

export interface HlsFeed {
  videoRef: React.RefObject<HTMLVideoElement>;
  offline: boolean;
  /** Which stream is actually on screen — 'sub' when the main would not play. */
  variant: 'main' | 'sub';
}

export function useHls(slug: string, staggerMs = 0, hasSub = false): HlsFeed {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [offline, setOffline] = useState(false);
  const [variant, setVariant] = useState<'main' | 'sub'>('main');
  // Stagger applies on mount only — index reshuffles must not restart streams.
  const delayRef = useRef(staggerMs);

  useEffect(() => {
    let disposed = false;
    let hls: Hls | null = null;
    const timers: number[] = [];
    // Which stream this attach is using. Held in a closure variable, not state:
    // the retry loop below needs the current value synchronously.
    let useSub = false;
    setVariant('main');

    const later = (fn: () => void, ms: number) =>
      timers.push(window.setTimeout(() => { if (!disposed) fn(); }, ms));

    const markOffline = () => {
      // One shot at the sub before giving up. A camera whose main this browser
      // cannot decode would otherwise retry the same unplayable stream every
      // 15 s forever, which is exactly what a black tile looks like.
      if (hasSub && !useSub) {
        useSub = true;
        setVariant('sub');
        later(attach, 500);
        return;
      }
      setOffline(true);
      // Retry while mounted — a camera coming back goes live by itself. Start
      // again from the main: whatever stopped it may be over, and the main is
      // the better picture when it works.
      useSub = false;
      setVariant('main');
      later(attach, 15000);
    };

    const attach = () => {
      const video = videoRef.current;
      if (!video) return;
      const src = hlsSrc(slug, useSub ? 'sub' : 'main');
      if (Hls.isSupported()) {
        try { hls?.destroy(); } catch { /* already gone */ }
        const h = new Hls({ liveSyncDurationCount: 3, maxBufferLength: 10 });
        hls = h;
        h.on(Hls.Events.MANIFEST_PARSED, () => {
          setOffline(false);
          video.play().catch(() => {});
        });
        h.on(Hls.Events.ERROR, (_e, data) => {
          if (!data.fatal) return;
          try { h.destroy(); } catch { /* already gone */ }
          if (hls === h) hls = null;
          markOffline();
        });
        h.loadSource(src);
        h.attachMedia(video);
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        // Safari-native HLS fallback.
        video.onerror = markOffline;
        video.src = src;
        video.play().catch(() => {});
      }
    };

    setOffline(false);
    later(attach, delayRef.current);
    return () => {
      disposed = true;
      timers.forEach(clearTimeout);
      try { hls?.destroy(); } catch { /* already gone */ }
      hls = null;
    };
  }, [slug, hasSub]);

  return { videoRef, offline, variant };
}
