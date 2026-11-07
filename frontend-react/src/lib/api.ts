/**
 * api.ts — authenticated fetch helpers (ported from the legacy apiFetch /
 * apiBlob). All product calls are same-origin under /api; errors surface the
 * backend's `detail` string.
 */
import { authHeaders } from './auth';

const API = '/api';

/**
 * A failed API call. `detail` keeps the backend's payload intact, because some
 * of it is structured and useful: the NVR's clip 404 carries the camera's
 * actual recorded range, which the playback UI turns into "jump to the nearest
 * footage" rather than a dead end.
 *
 * `message` is always something a human can read. Callers that render errors
 * verbatim must never see a JSON blob — that is exactly how a raw
 * `{"error":"Requested timestamp is outside recorded range",...}` ended up
 * painted across a playback tile.
 */
export class ApiError extends Error {
  status: number;
  detail: any;
  constructor(status: number, detail: any, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

function humanize(status: number, detail: any): string {
  if (typeof detail === 'string' && detail) return detail;
  // FastAPI lets a handler raise HTTPException(404, {...}); the NVR does, and
  // its dicts always carry a human sentence under `error`.
  if (detail && typeof detail === 'object' && typeof detail.error === 'string') return detail.error;
  // FastAPI's own 422: a list of {loc, msg}. Say what was wrong ("timezone: unknown timezone 'Asia/Calcutta'")
  // — a bare "Request failed (HTTP 422)" left a patrol unsaveable with no clue why (2026-10-27).
  if (Array.isArray(detail) && detail.length) {
    const parts = detail.slice(0, 3).map((d: any) => {
      const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== 'body' && typeof x === 'string').pop() : null;
      const msg = String(d?.msg || '').replace(/^Value error, /, '');
      return loc ? `${loc}: ${msg}` : msg;
    }).filter(Boolean);
    if (parts.length) return parts.join(' · ');
  }
  return `Request failed (HTTP ${status})`;
}

/**
 * Build an ApiError from a failed Response. Exported because coverage.ts needs
 * its own fetch (to read the X-NVR-Coverage header, which apiBlob hides) and
 * must not re-implement this — when it did, the two drifted and the playback
 * chunk fetcher went on rendering raw JSON long after this one stopped.
 */
export async function toApiError(resp: Response): Promise<ApiError> {
  let detail: any = null;
  try {
    const j = await resp.json();
    detail = j?.detail ?? j;
  } catch { /* no JSON body — fall through to the status message */ }
  return new ApiError(resp.status, detail, humanize(resp.status, detail));
}

async function doFetch(path: string, opts: RequestInit = {}): Promise<Response> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(await authHeaders()),
    ...(opts.headers as Record<string, string> | undefined),
  };
  const resp = await fetch(API + path, { credentials: 'same-origin', ...opts, headers });
  if (!resp.ok) throw await toApiError(resp);
  return resp;
}

export async function apiFetch<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const resp = await doFetch(path, opts);
  if (resp.status === 204) return null as T;
  return resp.json();
}

export async function apiBlob(path: string): Promise<Blob> {
  const resp = await doFetch(path);
  return resp.blob();
}

/**
 * Multipart upload (CSV bulk import). Content-Type is deliberately NOT set —
 * the browser has to add it itself so it can append the multipart boundary.
 */
export async function apiUpload<T = any>(path: string, form: FormData): Promise<T> {
  const resp = await fetch(API + path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: await authHeaders(),
    body: form,
  });
  if (!resp.ok) throw await toApiError(resp);
  return resp.json();
}

/** Blob download with the server-provided filename (Content-Disposition). */
export async function apiDownload(path: string, fallbackName: string, forceName = false): Promise<void> {
  const resp = await doFetch(path);
  const blob = await resp.blob();
  const m = (resp.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  // Two reports can share one server-side filename (both audit exports come
  // back as audit-log-<date>.csv) — forceName keeps them apart on disk.
  a.download = forceName || !m ? fallbackName : m[1];
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

/**
 * HLS playlist URL for a relay slug (Caddy path on TLS, direct port on HTTP).
 *
 * `variant: 'sub'` addresses the camera's low-resolution second stream, which
 * the relay publishes as `<slug>_sub` when that track is switched on. It is the
 * same path shape — the relay does not care which of a camera's streams it is
 * carrying — so this is only a name.
 */
export function hlsSrc(slug: string, variant: 'main' | 'sub' = 'main'): string {
  const path = variant === 'sub' ? `${slug}_sub` : slug;
  if (location.protocol === 'https:') return `/hls/${path}/index.m3u8`;
  return `http://${location.hostname}:8988/${path}/index.m3u8`;
}

/**
 * WHEP (WebRTC) URL for the low-latency focus / PTZ view, mediamtx's
 * http://<host>:8889/<path>/whep. Returns null when WebRTC should not be
 * attempted — today that is the HTTPS/remote path, which has no /whep proxy on
 * the optional Caddy front door yet, so it stays on HLS. On the on-prem LAN
 * (plain HTTP straight to the host) it returns the direct WHEP endpoint.
 */
export function whepSrc(slug: string, variant: 'main' | 'sub' = 'main'): string | null {
  if (location.protocol === 'https:') return null;
  const path = variant === 'sub' ? `${slug}_sub` : slug;
  return `http://${location.hostname}:8889/${path}/whep`;
}
