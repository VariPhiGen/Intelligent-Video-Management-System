/**
 * useChunkPlayer.ts — the single-camera chunked playback engine, ported from
 * the legacy pbSeek/pbPreloadNext/pbSwapToPreload/pbBind. Continuous playback
 * from 2-minute MP4 chunks fetched as authed blobs: preload the next chunk at
 * 80% progress, swap seamlessly on 'ended'. In-flight fetches carry an
 * AbortController; a token fences stale async resolves after camera switches.
 */
import { useCallback, useEffect, useRef, useState, type RefObject } from 'react';
import { CHUNK_SECONDS, fetchChunk } from './coverage';

export interface ChunkPlayerHandlers {
  /** True if `epochSec` falls in a known recording gap → playback pauses. */
  inGap?: (epochSec: number) => boolean;
  /** Server said the seeked 2-min window is fully covered (prune stale gaps). */
  onFullCoverage?: (epochSec: number) => void;
}

export interface ChunkPlayer {
  /** Wall-clock playhead, epoch seconds (null before the first clip loads). */
  wall: number | null;
  paused: boolean;
  loading: boolean;
  /** X-NVR-Coverage < 1 on the current chunk — recording gap in the window. */
  gapWarn: boolean;
  /** Message overlay over the video (null = hidden). */
  overlay: string | null;
  setOverlay: (msg: string | null) => void;
  /** Load + play the 2-min chunk starting at `epochSec`. */
  seek: (camera: string, epochSec: number) => void;
  /** Halt everything: abort fetches, revoke URLs, clear the video element. */
  stop: () => void;
  togglePlay: () => void;
  setRate: (r: number) => void;
  /** Current wall-clock position (clipStart + video.currentTime), or null. */
  currentEpoch: () => number | null;
  /** Pause + "No recording at this time" (click landed in a gap). */
  showGapStop: () => void;
}

export function useChunkPlayer(
  videoRef: RefObject<HTMLVideoElement | null>,
  handlers: ChunkPlayerHandlers = {},
  opts: { enabled?: boolean } = {},
): ChunkPlayer {
  // Both engines are constructed unconditionally (hooks can't be conditional)
  // and share one <video>. The idle one must not bind listeners or fetch, or
  // it fights the live one over the same element.
  const enabled = opts.enabled !== false;
  const [wall, setWall] = useState<number | null>(null);
  const [paused, setPaused] = useState(true);
  const [loading, setLoading] = useState(false);
  const [gapWarn, setGapWarn] = useState(false);
  const [overlay, setOverlay] = useState<string | null>('Click the timeline to start playback');

  const h = useRef(handlers);
  h.current = handlers;

  const token = useRef(0);                       // fences stale async resolves
  const cameraRef = useRef<string | null>(null);
  const clipStart = useRef<number | null>(null); // wall epoch (s) of video time 0
  const clipDur = useRef(0);
  const objUrl = useRef<string | null>(null);
  const fetchCtl = useRef<AbortController | null>(null);
  const preUrl = useRef<string | null>(null);
  const preStart = useRef<number | null>(null);
  const preLoading = useRef(false);
  const preCtl = useRef<AbortController | null>(null);
  const rate = useRef(1);

  const revokePreload = useCallback(() => {
    if (preUrl.current) { URL.revokeObjectURL(preUrl.current); preUrl.current = null; }
    preStart.current = null;
    preLoading.current = false;
  }, []);

  const stop = useCallback(() => {
    token.current++;
    if (fetchCtl.current) { fetchCtl.current.abort(); fetchCtl.current = null; }
    if (preCtl.current) { preCtl.current.abort(); preCtl.current = null; }
    const v = videoRef.current;
    if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    if (objUrl.current) { URL.revokeObjectURL(objUrl.current); objUrl.current = null; }
    revokePreload();
    clipStart.current = null;
    setWall(null);
    setLoading(false);
    setGapWarn(false);
  }, [revokePreload, videoRef]);

  const seek = useCallback(async (camera: string, epochSec: number) => {
    const v = videoRef.current;
    if (!v || !enabled) return;
    if (fetchCtl.current) fetchCtl.current.abort();
    const ctl = new AbortController();
    fetchCtl.current = ctl;
    if (preCtl.current) { preCtl.current.abort(); preCtl.current = null; }
    revokePreload();
    cameraRef.current = camera;
    const my = token.current;
    setOverlay(null);
    setLoading(true);
    setGapWarn(false);
    try {
      const { blob, coverage } = await fetchChunk(camera, epochSec, ctl.signal);
      if (my !== token.current || camera !== cameraRef.current) return;
      if (coverage < 1) setGapWarn(true);
      else h.current.onFullCoverage?.(epochSec); // window fully covered → stale gaps prune
      if (objUrl.current) URL.revokeObjectURL(objUrl.current);
      objUrl.current = URL.createObjectURL(blob);
      clipStart.current = epochSec;
      v.src = objUrl.current;
      v.playbackRate = rate.current;
      await v.play().catch(() => {});
      if (my !== token.current) return;
      setLoading(false);
    } catch (e: any) {
      if (e && e.name === 'AbortError') return;
      if (my !== token.current) return;
      setLoading(false);
      setOverlay(e?.message || 'Failed to load clip');
    }
  }, [enabled, revokePreload, videoRef]);

  const preloadNext = useCallback(async () => {
    if (clipStart.current == null || preLoading.current || preUrl.current || !cameraRef.current) return;
    preLoading.current = true;
    const ctl = new AbortController();
    preCtl.current = ctl;
    const my = token.current, cam = cameraRef.current;
    const next = clipStart.current + (clipDur.current || CHUNK_SECONDS);
    try {
      const { blob } = await fetchChunk(cam, next, ctl.signal);
      if (my !== token.current || cam !== cameraRef.current) return;
      if (preUrl.current) URL.revokeObjectURL(preUrl.current);
      preUrl.current = URL.createObjectURL(blob);
      preStart.current = next;
    } catch { /* aborted or gap — 'ended' just pauses */ }
    finally {
      preLoading.current = false;
      if (preCtl.current === ctl) preCtl.current = null;
    }
  }, []);

  const swapToPreload = useCallback(() => {
    const v = videoRef.current;
    if (!preUrl.current || !v) return;
    if (objUrl.current) URL.revokeObjectURL(objUrl.current);
    objUrl.current = preUrl.current;
    clipStart.current = preStart.current;
    preUrl.current = null;
    preStart.current = null;
    preLoading.current = false;
    v.src = objUrl.current;
    v.playbackRate = rate.current;
    v.play().catch(() => {});
  }, [videoRef]);

  // Video element event binding (element persists for the component's life).
  useEffect(() => {
    const v = videoRef.current;
    if (!v || !enabled) return;
    const onMeta = () => { clipDur.current = v.duration || 0; };
    const onPlay = () => setPaused(false);
    const onPause = () => setPaused(true);
    const onTime = () => {
      if (clipStart.current == null) return;
      const w = clipStart.current + v.currentTime;
      setWall(w);
      if (h.current.inGap?.(w)) {
        if (!v.paused) v.pause();
        if (preCtl.current) { preCtl.current.abort(); preCtl.current = null; }
        revokePreload();
        setOverlay('No recording at this time');
        return;
      }
      if (clipDur.current > 0 && v.currentTime / clipDur.current > 0.8) preloadNext();
    };
    const onEnded = () => { if (preUrl.current) swapToPreload(); };
    v.addEventListener('loadedmetadata', onMeta);
    v.addEventListener('play', onPlay);
    v.addEventListener('pause', onPause);
    v.addEventListener('timeupdate', onTime);
    v.addEventListener('ended', onEnded);
    return () => {
      v.removeEventListener('loadedmetadata', onMeta);
      v.removeEventListener('play', onPlay);
      v.removeEventListener('pause', onPause);
      v.removeEventListener('timeupdate', onTime);
      v.removeEventListener('ended', onEnded);
    };
  }, [enabled, preloadNext, revokePreload, swapToPreload, videoRef]);

  // Full halt + URL revocation on unmount.
  useEffect(() => () => { stop(); }, [stop]);

  const togglePlay = useCallback(() => {
    const v = videoRef.current;
    if (!v || !v.src) return;
    if (v.paused) v.play().catch(() => {});
    else v.pause();
  }, [videoRef]);

  const setRate = useCallback((r: number) => {
    rate.current = r;
    const v = videoRef.current;
    if (v) { try { v.playbackRate = r; } catch { /* not ready yet */ } }
  }, [videoRef]);

  const currentEpoch = useCallback((): number | null => {
    const v = videoRef.current;
    if (clipStart.current == null || !v) return null;
    return clipStart.current + v.currentTime;
  }, [videoRef]);

  const showGapStop = useCallback(() => {
    if (fetchCtl.current) { fetchCtl.current.abort(); fetchCtl.current = null; }
    const v = videoRef.current;
    if (v && !v.paused) v.pause();
    setLoading(false);
    setOverlay('No recording at this time');
  }, [videoRef]);

  return { wall, paused, loading, gapWarn, overlay, setOverlay, seek, stop, togglePlay, setRate, currentEpoch, showGapStop };
}
