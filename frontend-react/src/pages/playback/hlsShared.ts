/**
 * hlsShared.ts — the pieces both playback engines need.
 *
 * Playback of recorded footage is an HLS VOD playlist over the NVR's 60 s
 * MPEG-TS segments (see services/nvr/api/hls.py). Two callers drive it — the
 * single-camera player and the multi-camera tiles — and the parts they share
 * are the ones that are easy to get subtly wrong twice: the HEVC probe that
 * decides the server's cost, the bearer token that hls.js needs *synchronously*,
 * and the PROGRAM-DATE-TIME arithmetic that maps media time to wall clock.
 */
import { useEffect, useRef, type RefObject } from 'react';
import Hls, { type Fragment } from 'hls.js';
import { ApiError } from '@/lib/api';
import { authHeaders } from '@/lib/auth';

/** Seconds of footage per playlist. Long enough that crossing one is rare. */
export const WINDOW_SECONDS = 1800;
/**
 * Floor on how often a playlist may be rebuilt.
 *
 * Near the live edge a playlist stops at the last recorded segment, minutes
 * short of the window asked for, so the playhead sits permanently at its end.
 * Without this floor every timeupdate fires another rebuild — observed as the
 * same two segments re-fetched ~40 times in a few seconds.
 */
export const RELOAD_MIN_INTERVAL_MS = 15_000;

let hevcAnswer: boolean | null = null;

/**
 * Can this browser decode HEVC? Asked of MediaSource, not <video>, because
 * that is the decoder hls.js will actually feed.
 *
 * The answer picks between two very different server costs — true lets the NVR
 * remux (`-c copy`, ~0.06 s/segment), false makes it transcode. Never infer it
 * from the User-Agent: a wrong `true` gives a black player.
 */
export function detectHevc(): boolean {
  if (hevcAnswer !== null) return hevcAnswer;
  const types = [
    'video/mp4; codecs="hvc1.1.6.L93.B0"',
    'video/mp4; codecs="hev1.1.6.L93.B0"',
  ];
  const MS: typeof MediaSource | undefined =
    (window as any).ManagedMediaSource ?? window.MediaSource;
  hevcAnswer = MS && typeof MS.isTypeSupported === 'function'
    ? types.some(t => MS.isTypeSupported(t))
    : types.some(t => document.createElement('video').canPlayType(t) !== '');
  return hevcAnswer;
}

/** Playlist URL for `windowSeconds` of footage starting at `fromEpochSec`. */
export function playlistUrl(
  camera: string, fromEpochSec: number, hevc: boolean, windowSeconds = WINDOW_SECONDS,
): string {
  const iso = (s: number) => new Date(s * 1000).toISOString();
  return `/api/nvr/hls/${encodeURIComponent(camera)}/index.m3u8`
    + `?from=${encodeURIComponent(iso(fromEpochSec))}`
    + `&to=${encodeURIComponent(iso(fromEpochSec + windowSeconds))}`
    + `&hevc=${hevc}`;
}

/**
 * A ref holding the current `Authorization` header value, kept fresh.
 *
 * hls.js's xhrSetup is synchronous, so the token has to already be in hand when
 * a segment is requested — `authHeaders()` is async because it may refresh.
 * Keycloak renews well before expiry, so a slow poll suffices; callers also
 * refresh directly before each seek.
 */
export function useBearerToken(enabled: boolean): {
  bearer: RefObject<string>; refresh: () => Promise<void>;
} {
  const bearer = useRef('');
  const refresh = useRef(async () => {
    try {
      bearer.current = (await authHeaders()).Authorization || '';
    } catch { /* auth.tsx already redirects to login on a dead session */ }
  }).current;
  useEffect(() => {
    if (!enabled) return;
    refresh();
    const id = setInterval(refresh, 20_000);
    return () => clearInterval(id);
  }, [enabled, refresh]);
  return { bearer, refresh };
}

/** A configured hls.js instance that authenticates every request it makes. */
export function createHls(bearer: RefObject<string>, opts: {
  maxBufferLength?: number; backBufferLength?: number;
} = {}): Hls {
  return new Hls({
    xhrSetup: (xhr) => {
      if (bearer.current) xhr.setRequestHeader('Authorization', bearer.current);
    },
    // VOD over a LAN: buffer enough that a slow segment build (the transcode
    // mode) doesn't stall the playhead, but bound the back buffer so scrubbing
    // a long window doesn't grow memory without limit. Tiles pass smaller
    // numbers — four of these run at once.
    maxBufferLength: opts.maxBufferLength ?? 60,
    backBufferLength: opts.backBufferLength ?? 90,
    lowLatencyMode: false,
  });
}

/** Fragments that actually carry a PROGRAM-DATE-TIME — the rest are unusable. */
export function datedFragments(frags: Fragment[] | undefined): Fragment[] {
  return (frags || []).filter(f => f.programDateTime != null);
}

/** Wall-clock epoch (seconds) for a position on the media timeline. */
export function epochAt(frags: Fragment[], mediaTime: number): number | null {
  for (const f of frags) {
    if (mediaTime >= f.start && mediaTime < f.start + f.duration) {
      return f.programDateTime! / 1000 + (mediaTime - f.start);
    }
  }
  return null;
}

/**
 * Media-timeline position for a wall-clock instant.
 *
 * A target inside a recording gap lands at the start of the next fragment
 * rather than failing — the playlist simply has no footage there, and jumping
 * forward is what an operator scrubbing across a gap expects.
 */
export function mediaTimeFor(frags: Fragment[], targetEpoch: number): number | null {
  if (!frags.length) return null;
  for (const f of frags) {
    const start = f.programDateTime! / 1000;
    if (targetEpoch < start) return f.start;
    if (targetEpoch < start + f.duration) return f.start + (targetEpoch - start);
  }
  const last = frags[frags.length - 1];
  return last.start + last.duration - 0.1;
}

/** Wall-clock epoch one past the last fragment — the end of what this playlist covers. */
export function playlistEnd(frags: Fragment[]): number | null {
  if (!frags.length) return null;
  const last = frags[frags.length - 1];
  return last.programDateTime! / 1000 + last.duration;
}

/** True when the fragments cover their span with no gap worth warning about. */
export function coverageComplete(frags: Fragment[]): boolean {
  const end = playlistEnd(frags);
  if (end == null) return false;
  const spanned = end - frags[0].programDateTime! / 1000;
  const covered = frags.reduce((n, f) => n + f.duration, 0);
  return spanned > 0 && covered / spanned > 0.99;
}

/**
 * Turn an hls.js error into the same `ApiError` a direct fetch would have
 * raised, so callers keep one way of classifying "no footage here".
 *
 * The NVR's playlist 404 carries the identical `recorded_range` body as
 * `/clip`'s, which is what lets a tile offer a jump to real footage.
 */
export function hlsResponseError(data: { response?: { code?: number; data?: unknown } }): ApiError {
  const code = data.response?.code ?? 0;
  let detail: any = null;
  const body = data.response?.data;
  if (typeof body === 'string') {
    try { detail = JSON.parse(body)?.detail ?? JSON.parse(body); } catch { detail = body; }
  } else if (body && typeof body === 'object') {
    detail = (body as any).detail ?? body;
  }
  const msg = typeof detail === 'string' ? detail
    : detail?.error || `Request failed (HTTP ${code})`;
  return new ApiError(code, detail, msg);
}
