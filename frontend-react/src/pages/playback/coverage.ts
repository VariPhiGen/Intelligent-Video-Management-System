/**
 * coverage.ts — NVR coverage/clip helpers for the playback pages (ported from
 * the legacy nvrCoverage/covSpans/pbFetch). Timestamps from the NVR may be
 * epoch seconds (numbers) or ISO strings — toMs/toSec normalise both.
 */
import { apiFetch, toApiError } from '@/lib/api';
import { authHeaders } from '@/lib/auth';
import type { NvrCoverage } from '@/lib/types';

/** One row of GET /api/nvr/cameras. */
export interface NvrCamera {
  name: string;
  recording: boolean;
  earliest: string | null;
  latest: string | null;
  storage_bytes: number | null;
}

/** Clip chunk length (seconds) — the whole playback engine is built on it. */
export const CHUNK_SECONDS = 120;

/** Epoch seconds (number) or ISO string → epoch milliseconds. */
export function toMs(v: number | string): number {
  return typeof v === 'number' ? v * 1000 : Date.parse(v);
}
/** Epoch seconds (number) or ISO string → epoch seconds. */
export function toSec(v: number | string): number {
  return toMs(v) / 1000;
}

/** GET /api/nvr/coverage for a camera over [from, to]. */
export function nvrCoverage(
  slug: string, from: Date, to: Date, minGapSeconds?: number, signal?: AbortSignal,
): Promise<NvrCoverage> {
  const min = minGapSeconds != null ? `&min_gap_seconds=${minGapSeconds}` : '';
  return apiFetch(
    `/nvr/coverage?camera=${encodeURIComponent(slug)}` +
    `&from=${encodeURIComponent(from.toISOString())}&to=${encodeURIComponent(to.toISOString())}${min}`,
    signal ? { signal } : {},
  );
}

/** Covered spans (ms pairs) = window ∩ [earliest, latest] minus gaps. */
export function covSpans(cov: NvrCoverage | null | undefined, winS: number, winE: number): [number, number][] {
  if (!cov || cov.earliest == null || cov.latest == null) return [];
  const s = Math.max(winS, toMs(cov.earliest));
  const e = Math.min(winE, toMs(cov.latest));
  if (e <= s) return [];
  let spans: [number, number][] = [[s, e]];
  for (const g of cov.gaps || []) {
    const gs = toMs(g.start), ge = toMs(g.end);
    spans = spans.flatMap(([a, b]): [number, number][] => {
      if (ge <= a || gs >= b) return [[a, b]];
      const out: [number, number][] = [];
      if (gs > a) out.push([a, gs]);
      if (ge < b) out.push([ge, b]);
      return out;
    });
  }
  return spans;
}

/**
 * Authenticated fetch that exposes the raw Response (headers + abort signal) —
 * apiBlob hides headers, but the clip endpoint reports window coverage in
 * X-NVR-Coverage. Modeled on lib/api.ts doFetch, implemented locally.
 */
async function nvrFetch(path: string, signal?: AbortSignal): Promise<Response> {
  const headers: Record<string, string> = await authHeaders();
  const resp = await fetch('/api' + path, { credentials: 'same-origin', headers, signal });
  // Same ApiError as lib/api.ts — shared on purpose, so a structured NVR 404
  // (recorded_range &c.) survives to the UI here too instead of being flattened.
  if (!resp.ok) throw await toApiError(resp);
  return resp;
}

/** Fetch one 2-minute MP4 chunk starting at `epochSec`, with its coverage. */
export async function fetchChunk(
  camera: string, epochSec: number, signal?: AbortSignal,
): Promise<{ blob: Blob; coverage: number }> {
  const ts = new Date(epochSec * 1000).toISOString();
  const resp = await nvrFetch(
    `/nvr/clip?camera=${encodeURIComponent(camera)}&timestamp=${encodeURIComponent(ts)}&before=0&after=${CHUNK_SECONDS}`,
    signal,
  );
  const coverage = parseFloat(resp.headers.get('X-NVR-Coverage') || '1');
  return { blob: await resp.blob(), coverage };
}

/** Wall-clock formatter for the playhead timestamp (local time). */
export function fmtWall(epochSec: number): string {
  const d = new Date(epochSec * 1000), p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
