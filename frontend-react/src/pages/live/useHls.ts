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
import { hlsSrc, whepSrc } from '@/lib/api';
import { detectHevc } from '@/pages/playback/hlsShared';
import { attachWhep, type WhepSession } from './whep';

/** An HEVC codec string as a playlist's CODECS attribute carries it. */
const isHevc = (codec: string | undefined) => !!codec && /^(hvc1|hev1)/i.test(codec);

export interface HlsFeed {
  videoRef: React.RefObject<HTMLVideoElement>;
  offline: boolean;
  /** Which stream is actually on screen — 'sub' when the main would not play. */
  variant: 'main' | 'sub';
  /** Offline because every stream this camera offers is H.265 and this
   *  browser cannot decode H.265. The camera is fine; "Signal lost" would send
   *  the operator looking for a network fault that does not exist. */
  unsupportedCodec: boolean;
}

export function useHls(slug: string, staggerMs = 0, hasSub = false,
                       preferSub = false, lowLatency = false,
                       preferWebRtc = false): HlsFeed {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [offline, setOffline] = useState(false);
  const [variant, setVariant] = useState<'main' | 'sub'>('main');
  const [unsupportedCodec, setUnsupportedCodec] = useState(false);
  // Stagger applies on mount only — index reshuffles must not restart streams.
  const delayRef = useRef(staggerMs);

  useEffect(() => {
    let disposed = false;
    let hls: Hls | null = null;
    let whep: WhepSession | null = null;
    // Once WebRTC has failed for this camera we do not keep retrying it on the
    // 15 s loop — the fallback to HLS is sticky until the next mount.
    let webrtcFailed = false;
    const timers: number[] = [];
    // Which stream this attach is using. Held in a closure variable, not state:
    // the retry loop below needs the current value synchronously.
    //
    // `preferSub` STARTS on the low-res track rather than falling back to it.
    // A wall cell painted 380px wide still decodes the camera's full main
    // stream otherwise — the reason the wall was capped at nine monitors. The
    // picture an operator gets is the one that fits the tile; the LOW-RES badge
    // says so, and the expanded view is always the main stream.
    let useSub = preferSub && hasSub;
    // One swap per attach cycle. Whichever stream we started on, the other one
    // gets exactly one try before the tile is called offline.
    let triedOther = false;
    // Streams in this attach cycle that were refused for being H.265 in a
    // browser without an H.265 decoder. When every stream tried is in here, the
    // tile says so rather than "Signal lost".
    const hevcRefused = new Set<'main' | 'sub'>();
    setVariant(useSub ? 'sub' : 'main');

    const later = (fn: () => void, ms: number) =>
      timers.push(window.setTimeout(() => { if (!disposed) fn(); }, ms));
    // Stall watchdog (grid / wall): a tile whose picture hasn't moved for 25 s while it should be playing is torn down
    // and re-attached. hls.js only gives up on FATAL errors; a decoder that wedges, or a live edge it never catches up
    // to, just freezes the tile forever — on a wall left running for days, one by one. Cleared on every re-attach.
    let watch: number | undefined;
    const stopWatch = () => { if (watch !== undefined) { window.clearInterval(watch); watch = undefined; } };

    const markOffline = () => {
      // One shot at the OTHER stream before giving up. A camera whose main this
      // browser cannot decode would otherwise retry the same unplayable stream
      // every 15 s forever, which is exactly what a black tile looks like. It
      // runs both ways: a wall cell that asked for the sub and found none falls
      // up to the main rather than showing nothing.
      if (hasSub && !triedOther) {
        triedOther = true;
        useSub = !useSub;
        setVariant(useSub ? 'sub' : 'main');
        later(attach, 500);
        return;
      }
      const tried = hasSub ? 2 : 1;
      setUnsupportedCodec(hevcRefused.size >= tried);
      hevcRefused.clear();
      setOffline(true);
      // Retry while mounted — a camera coming back goes live by itself. Start
      // again from the stream this tile actually wants: whatever stopped it may
      // be over, and the preferred stream is the right picture when it works.
      triedOther = false;
      useSub = preferSub && hasSub;
      setVariant(useSub ? 'sub' : 'main');
      later(attach, 15000);
    };

    const attachHls = () => {
      const video = videoRef.current;
      if (!video) return;
      const src = hlsSrc(slug, useSub ? 'sub' : 'main');
      if (Hls.isSupported()) {
        try { hls?.destroy(); } catch { /* already gone */ }
        // Two profiles off the SAME mediamtx LL-HLS source (200ms parts):
        //
        //   grid   liveSyncDurationCount 3 + a 10s buffer — sit a few seconds
        //          behind live so a wall of tiles rides out network jitter
        //          without stalling. Latency does not matter on a monitoring
        //          wall; a frozen tile does.
        //   focus  lowLatencyMode ON, target ~1s behind live, short buffer.
        //          This is the expanded / PTZ view, where the operator is
        //          steering by the picture and a 3s delay makes them overshoot.
        //          hls.js was ignoring mediamtx's low-latency parts entirely
        //          until this flag — the parts were produced and never used.
        //          The cost is less cushion, so it is scoped to the one view
        //          that needs it, not the grid.
        const h = lowLatency
          ? new Hls({
              lowLatencyMode: true,
              liveSyncDurationCount: 2,
              maxBufferLength: 4,
              backBufferLength: 4,
              // Nudge playback slightly fast to close a latency gap instead of
              // jumping (a jump on a PTZ feed reads as a glitch mid-move).
              maxLiveSyncPlaybackRate: 1.5,
            })
          // lowLatencyMode must be OFF here explicitly — hls.js defaults it ON, so every grid / wall tile was doing
          // LL-HLS blocking playlist reloads every ~200 ms part (16 tiles ≈ 40+ playlist requests a second, measured
          // on kec01), for latency a monitoring wall doesn't need. And bound the buffers: backBufferLength defaults
          // to Infinity, so played video was never released — hours × 16 tiles until an Android WebView ran out of
          // memory and went white.
          : new Hls({
              lowLatencyMode: false,
              liveSyncDurationCount: 3,
              maxBufferLength: 10,
              backBufferLength: 10,
              maxBufferSize: 20 * 1000 * 1000,
            });
        hls = h;
        stopWatch();
        if (!lowLatency) {
          let lastT = -1, still = 0;
          watch = window.setInterval(() => {
            if (disposed || hls !== h || video.paused && video.readyState < 2) { still = 0; return; }
            if (video.currentTime === lastT) still += 5; else { still = 0; lastT = video.currentTime; }
            if (still >= 25) {
              stopWatch();
              try { h.destroy(); } catch { /* already gone */ }
              if (hls === h) hls = null;
              attach();
            }
          }, 5000);
        }
        // First-picture watchdog: a stream the browser accepts but can't actually decode (H.265 on many Android
        // tablets, or 2560×1440 beyond the device's decoder) gives NO error — just a black tile forever. No frame in
        // 10 s = treat it as failed: markOffline() swaps to the other stream once (main ↔ sub), and if an H.265
        // stream is what failed, the tile says the codec is unsupported instead of "Signal lost".
        let streamIsHevc = false;
        later(() => {
          if (hls !== h || video.readyState >= 2 || video.videoWidth > 0) return;
          if (streamIsHevc) hevcRefused.add(useSub ? 'sub' : 'main');
          stopWatch();
          try { h.destroy(); } catch { /* already gone */ }
          if (hls === h) hls = null;
          markOffline();
        }, 10000);
        h.on(Hls.Events.MANIFEST_PARSED, (_e, data) => {
          streamIsHevc = data.levels.length > 0 && data.levels.every(l => isHevc(l.videoCodec));
          // Refuse an H.265 stream this browser cannot decode HERE, from the
          // playlist's CODECS attribute. Fed to MSE anyway it fails silently
          // (readyState 0, sometimes no error at all), so the fallback to the
          // sub stream — often H.264 — would wait on a timeout that may never
          // come.
          if (data.levels.length > 0 && data.levels.every(l => isHevc(l.videoCodec))
              && !detectHevc()) {
            hevcRefused.add(useSub ? 'sub' : 'main');
            try { h.destroy(); } catch { /* already gone */ }
            if (hls === h) hls = null;
            markOffline();
            return;
          }
          setOffline(false);
          setUnsupportedCodec(false);
          video.play().catch(() => {});
        });
        h.on(Hls.Events.ERROR, (_e, data) => {
          if (!data.fatal) return;
          // hls.js drops every level whose CODECS the browser's MSE refuses and,
          // with none left, fails here — before MANIFEST_PARSED ever fires. On
          // this fleet that is an H.265 stream in a browser without H.265.
          if (data.details === Hls.ErrorDetails.MANIFEST_INCOMPATIBLE_CODECS_ERROR
              && !detectHevc()) {
            hevcRefused.add(useSub ? 'sub' : 'main');
          }
          stopWatch();
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

    // The focus / PTZ view tries WebRTC first (mediamtx WHEP, ~200–500 ms) and
    // falls back to HLS on any failure, so a browser or network that cannot do
    // WebRTC — or the HTTPS/remote path, where whepSrc returns null — still
    // gets a picture. Everything else (offline flag, 15 s retry, sub-stream
    // fallback) is unchanged and shared, because a WebRTC failure just calls
    // attachHls().
    const attach = () => {
      const video = videoRef.current;
      if (!video) return;
      const whepUrl = preferWebRtc && !webrtcFailed && !useSub
        ? whepSrc(slug, 'main')
        : null;
      if (!whepUrl) { attachHls(); return; }

      attachWhep(video, whepUrl)
        .then(session => {
          if (disposed) { session.close(); return; }
          whep = session;
          setOffline(false);
          setUnsupportedCodec(false);
          // A camera that drops mid-session (network blip, relay restart) must
          // recover: tear the peer down and rejoin the normal retry path.
          session.pc.addEventListener('connectionstatechange', () => {
            if (disposed) return;
            const st = session.pc.connectionState;
            if (st === 'failed' || st === 'disconnected' || st === 'closed') {
              try { session.close(); } catch { /* gone */ }
              if (whep === session) whep = null;
              webrtcFailed = true;   // next attempt goes straight to HLS
              markOffline();
            }
          });
        })
        .catch(() => {
          // WebRTC did not come up — fall back to HLS for the rest of this
          // mount. This is the browser-can't-WebRTC and the whep-unreachable
          // path, and it must be seamless.
          if (disposed) return;
          webrtcFailed = true;
          attachHls();
        });
    };

    setOffline(false);
    later(attach, delayRef.current);
    return () => {
      disposed = true;
      stopWatch();
      timers.forEach(clearTimeout);
      try { whep?.close(); } catch { /* already gone */ }
      try { hls?.destroy(); } catch { /* already gone */ }
      hls = null;
    };
  }, [slug, hasSub, preferSub, lowLatency, preferWebRtc]);

  return { videoRef, offline, variant, unsupportedCodec };
}
