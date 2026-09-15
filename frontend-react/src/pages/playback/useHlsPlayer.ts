/**
 * useHlsPlayer.ts — playback driven by an HLS VOD playlist over the recorded
 * segments, replacing the fetch-a-whole-clip-per-seek engine.
 *
 * Why: useChunkPlayer asks the NVR to build a fresh 120 s MP4 on every seek and
 * then waits for the *entire* file (`await resp.blob()`) before showing frame
 * one — measured at ~54 s of server CPU per clip on a 1080p HEVC camera, which
 * is both the "playback takes forever" complaint and the 503 "extraction at
 * capacity" cascade. The recorder already writes 60 s MPEG-TS segments, so the
 * NVR can hand out a playlist over files that exist and the player fetches only
 * the minute it is about to show. Seeking becomes an HTTP GET.
 *
 * Interface-compatible with useChunkPlayer on purpose: SinglePlayer picks one
 * without knowing how either works.
 *
 * Requires MSE (hls.js). iOS Safari has no MediaSource, and its native HLS
 * player cannot attach the bearer token our proxy requires — so `available` is
 * false there and SinglePlayer keeps the clip engine for those clients.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import Hls, { type Fragment } from 'hls.js';
import { CHUNK_SECONDS } from './coverage';
import {
  RELOAD_MIN_INTERVAL_MS, WINDOW_SECONDS, coverageComplete, createHls, datedFragments,
  detectHevc, epochAt as epochAtFrags, mediaTimeFor, playlistEnd, playlistUrl, useBearerToken,
} from './hlsShared';
import type { ChunkPlayer, ChunkPlayerHandlers } from './useChunkPlayer';

/** Rebuild the playlist once the playhead is this close to its end. */
const RELOAD_MARGIN_S = 90;

export interface HlsPlayer extends ChunkPlayer {
  /** False when the browser has no MSE — caller must use the clip engine. */
  available: boolean;
  /** Delivery path the NVR chose: 'ts' | 'fmp4' | 'h264' (null until loaded). */
  mode: string | null;
}

export function useHlsPlayer(
  videoRef: RefObject<HTMLVideoElement | null>,
  handlers: ChunkPlayerHandlers = {},
  opts: { enabled?: boolean } = {},
): HlsPlayer {
  const enabled = opts.enabled !== false;
  const available = useMemo(() => Hls.isSupported(), []);
  const hevc = useMemo(detectHevc, []);

  const [wall, setWall] = useState<number | null>(null);
  const [paused, setPaused] = useState(true);
  const [loading, setLoading] = useState(false);
  const [gapWarn, setGapWarn] = useState(false);
  const [mode, setMode] = useState<string | null>(null);
  const [overlay, setOverlay] = useState<string | null>('Click the timeline to start playback');

  const h = useRef(handlers);
  h.current = handlers;

  const hlsRef = useRef<Hls | null>(null);
  const token = useRef(0);                    // fences stale async resolves
  const cameraRef = useRef<string | null>(null);
  const fragsRef = useRef<Fragment[]>([]);
  const windowEnd = useRef<number | null>(null);   // wall epoch of playlist end
  const windowWanted = useRef<number | null>(null); // wall epoch we asked for
  const pendingSeek = useRef<number | null>(null); // applied once frags arrive
  const reloading = useRef(false);
  const lastReload = useRef(0);
  const rate = useRef(1);
  const { bearer, refresh: refreshToken } = useBearerToken(enabled && available);

  const epochAt = useCallback(
    (mediaTime: number) => epochAtFrags(fragsRef.current, mediaTime), []);

  const currentEpoch = useCallback((): number | null => {
    const v = videoRef.current;
    if (!v) return null;
    return epochAt(v.currentTime);
  }, [epochAt, videoRef]);

  const teardown = useCallback(() => {
    const inst = hlsRef.current;
    hlsRef.current = null;
    if (inst) { try { inst.destroy(); } catch { /* already gone */ } }
    fragsRef.current = [];
    windowEnd.current = null;
    windowWanted.current = null;
    pendingSeek.current = null;
    reloading.current = false;
  }, []);

  const stop = useCallback(() => {
    token.current++;
    teardown();
    const v = videoRef.current;
    if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    cameraRef.current = null;
    setWall(null);
    setLoading(false);
    setGapWarn(false);
    setMode(null);
  }, [teardown, videoRef]);

  /** Position the video at `target` once the playlist's fragments are known. */
  const applySeek = useCallback((target: number) => {
    const v = videoRef.current;
    const media = mediaTimeFor(fragsRef.current, target);
    if (!v || media == null) return;
    v.currentTime = media;
    v.playbackRate = rate.current;
    v.play().catch(() => {});
    // NOT setLoading(false) here. This runs on LEVEL_LOADED — the *playlist*,
    // which is an index query and arrives in ~0 ms. The wait the operator
    // actually sees is the first segment: up to a few seconds when it has to be
    // transcoded. Clearing the spinner here made it blink off and leave a black
    // frame for the whole real wait. The video element tells us when it can
    // actually show something; that is what clears it (see the events below).
  }, [videoRef]);

  const seek = useCallback(async (camera: string, epochSec: number) => {
    const v = videoRef.current;
    if (!v || !available) return;
    const my = ++token.current;
    cameraRef.current = camera;
    setOverlay(null);
    setLoading(true);
    setGapWarn(false);
    await refreshToken();
    if (my !== token.current) return;

    teardown();
    windowWanted.current = epochSec + WINDOW_SECONDS;
    lastReload.current = Date.now();
    const url = playlistUrl(camera, epochSec, hevc);

    const inst = createHls(bearer);
    hlsRef.current = inst;
    pendingSeek.current = epochSec;

    inst.on(Hls.Events.MANIFEST_LOADED, (_e, data) => {
      // The NVR reports which of the three delivery paths this camera landed
      // on. Surfacing it is how a silent fall to per-segment transcoding stays
      // visible instead of just feeling slow.
      const xhr = data.networkDetails as XMLHttpRequest | undefined;
      try { setMode(xhr?.getResponseHeader?.('X-NVR-HLS-Mode') || null); } catch { /* opaque */ }
    });

    inst.on(Hls.Events.LEVEL_LOADED, (_e, data) => {
      if (my !== token.current) return;
      const frags = datedFragments(data.details.fragments);
      fragsRef.current = frags;
      if (!frags.length) { setOverlay('No recording at this time'); setLoading(false); return; }
      windowEnd.current = playlistEnd(frags);

      // A hole between consecutive PDTs is a recording gap the playlist simply
      // skips over; warn for it the way the clip engine warns on coverage < 1.
      const complete = coverageComplete(frags);
      setGapWarn(!complete);
      if (complete && pendingSeek.current != null && windowEnd.current != null
          && windowEnd.current - pendingSeek.current >= CHUNK_SECONDS) {
        h.current.onFullCoverage?.(pendingSeek.current);
      }

      if (pendingSeek.current != null) {
        applySeek(pendingSeek.current);
        pendingSeek.current = null;
      }
      reloading.current = false;
    });

    inst.on(Hls.Events.ERROR, (_e, data) => {
      if (my !== token.current) return;
      const status = (data.response as { code?: number } | undefined)?.code;
      if (status === 409) {
        // Grooming rewrote a segment under us, so the playlist's content
        // signatures are stale. Rebuild it from where we are.
        const at = currentEpoch() ?? pendingSeek.current;
        if (at != null && !reloading.current) {
          reloading.current = true;
          seek(camera, at);
        }
        return;
      }
      if (status === 401) { refreshToken().then(() => inst.startLoad()); return; }
      if (!data.fatal) return;
      teardown();
      reloading.current = false;
      setLoading(false);
      setOverlay(
        data.details === Hls.ErrorDetails.MANIFEST_LOAD_ERROR
          ? 'No recording at this time'
          : 'Playback failed',
      );
    });

    inst.loadSource(url);
    inst.attachMedia(v);
  }, [applySeek, available, currentEpoch, hevc, refreshToken, teardown, videoRef]);

  // ── video element events (the element outlives this hook) ────────────────
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !enabled || !available) return;
    const onPlay = () => setPaused(false);
    const onPause = () => setPaused(true);
    // Frames are decodable now — this, not the playlist, is when the wait ends.
    const onReady = () => setLoading(false);
    // Buffer ran dry mid-playback (the next segment is still being built):
    // put the spinner back rather than showing a frozen frame.
    const onWaiting = () => setLoading(true);
    const onTime = () => {
      const at = epochAt(v.currentTime);
      if (at == null) return;
      setWall(at);
      if (h.current.inGap?.(at)) return;
      // Extend the window before the playlist runs out, so continuous playback
      // crosses a boundary every 30 minutes instead of every 2.
      const end = windowEnd.current;
      const wanted = windowWanted.current;
      const cam = cameraRef.current;
      if (end == null || wanted == null || !cam || reloading.current) return;
      // A playlist that stops short of what we asked for has hit the end of the
      // recording, not the end of the window: rebuilding it yields the same
      // segments. Wait until the playhead is actually at the edge, and rate-limit,
      // so the live edge picks up new footage without spinning.
      const truncated = end < wanted - 1;
      const due = truncated
        ? at >= end - 1 && Date.now() - lastReload.current > RELOAD_MIN_INTERVAL_MS
        : at > end - RELOAD_MARGIN_S;
      if (due) {
        reloading.current = true;
        seek(cam, at);
      }
    };
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('canplay', onReady);
    v.addEventListener('playing', onReady);
    v.addEventListener('waiting', onWaiting);
    return () => {
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('canplay', onReady);
      v.removeEventListener('playing', onReady);
      v.removeEventListener('waiting', onWaiting);
    };
  }, [enabled, available, epochAt, seek, videoRef]);

  useEffect(() => () => { teardown(); }, [teardown]);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) v.play().catch(() => {}); else v.pause();
  }, [videoRef]);

  const setRate = useCallback((r: number) => {
    rate.current = r;
    const v = videoRef.current;
    if (v) v.playbackRate = r;
  }, [videoRef]);

  const showGapStop = useCallback(() => {
    videoRef.current?.pause();
    setOverlay('No recording at this time');
  }, [videoRef]);

  return {
    available, mode, wall, paused, loading, gapWarn, overlay, setOverlay,
    seek, stop, togglePlay, setRate, currentEpoch, showGapStop,
  };
}
