/**
 * smartsearch.ts — client for Smart Search.
 *
 * Everything goes through `/api/search/*` on this VMS, same-origin and behind
 * the product login, like every other call in the app. The browser no longer
 * talks to the CLIP index directly: the index is shared with the analytics
 * appliance and holds crops from cameras this VMS does not record, so scoping
 * it in the browser would mean shipping those hits to the client and merely
 * not drawing them.
 *
 * The backend (`routers/search.py`) resolves each hit against the camera
 * registry, joins the recorder's retained window and segment coverage, enforces
 * the `smart_search` permission, and audits the query. What arrives here is
 * only what this VMS can actually play back — plus a count of what it withheld,
 * so a short page stays explainable.
 *
 * Read-only by design: search and stats, nothing that mutates the index.
 */
import { apiFetch, toApiError } from './api';
import { authHeaders } from './auth';

/** The registry camera a hit belongs to. Always present — hits that resolve to
 *  no camera never reach the client. */
export interface SearchCamera {
  id: string;
  slug: string;
  name: string;
  /** Whether Smart Search indexes this camera. Present on the filter list; a
   *  hit's own camera reference omits it (a hit exists, so it was indexed).
   *  Load-bearing: without it an opted-out camera returns an empty result set
   *  that reads as "this person was never here". */
  search_indexing?: boolean;
}

/** A person crop indexed off the `person_crops` Kafka topic. */
export interface PersonHit {
  id: string;
  score: number;
  camera: SearchCamera;
  /** What the index called the camera — shown only when it differs from the slug. */
  sensor_id: string | null;
  /** Epoch milliseconds, normalised server-side. */
  when_ms: number;
  /** `camera:nonce.epoch:n` — see analytics/tracking.py. A string; older rows
   *  have none. */
  tracker_id: string | null;
  /** Whether the index kept the whole source frame for this row, so the card
   *  can show it with the match boxed instead of the crop. */
  has_frame?: boolean | null;
  /** Observations of this tracked object the search folded into this result. */
  sightings?: number | null;
  confidence: number | null;
  frame_number: number | null;
  pad_index: number | null;
  /** Normalised [x1, y1, x2, y2] in 0..1. */
  bbox: number[] | null;
}

/** A vehicle indexed via the edge ANPR ingest. */
export interface VehicleHit {
  id: string;
  score: number;
  camera: SearchCamera;
  sensor_id: string | null;
  when_ms: number;
  plate: string | null;
  /** The DETECTOR's confidence in `vehicle_type`, 0..1 — not the CLIP score,
   *  and not the plate's. Where several observations were collapsed it is the
   *  best one for the class they voted on (index/queries.py). */
  confidence: number | null;
  vehicle_type: string | null;
  color: string | null;
  brand: string | null;
  vehicle_speed: string | null;
  violation: string | null;
  /** `camera:nonce.epoch:n` — stored since migration 004, surfaced since the
   *  result cards started showing it. */
  tracker_id?: string | null;
  /** The matched detection, normalised 0-1 of the source frame. */
  bbox?: number[] | null;
  /** Whether the index kept the whole source frame for this row. */
  has_frame?: boolean | null;
  /** Observations of this tracked object the search folded into this result. */
  sightings?: number | null;
}

/** Why the backend withheld hits, keyed by reason → count. */
export type Withheld = Partial<Record<
  'unmapped' | 'not_recorded' | 'no_timestamp' | 'outside_retention' | 'in_gap',
  number
>>;

export interface SearchResponse<T> {
  results: T[];
  withheld: Withheld;
  /** How many the index matched before scoping — the denominator for `withheld`. */
  matched: number;
  /** More playable hits exist than the requested page size. */
  truncated: boolean;
}

/** Operator-facing wording, phrased to read after a count ("54 on cameras…"). */
export const WITHHELD_LABEL: Record<keyof Withheld, string> = {
  unmapped: 'on cameras not in this VMS',
  not_recorded: 'on cameras this VMS does not record',
  no_timestamp: 'without a timestamp',
  outside_retention: 'outside retained footage',
  in_gap: 'in a recording gap',
};

/** "2 on cameras not in this VMS, 1 without a timestamp". */
export function summariseWithheld(withheld: Withheld): { total: number; text: string } {
  const order: Array<keyof Withheld> = [
    'unmapped', 'not_recorded', 'outside_retention', 'in_gap', 'no_timestamp',
  ];
  const parts: string[] = [];
  let total = 0;
  for (const reason of order) {
    const n = withheld[reason];
    if (!n) continue;
    total += n;
    parts.push(`${n} ${WITHHELD_LABEL[reason]}`);
  }
  return { total, text: parts.join(', ') };
}

/** WHICH field arranges the page, and in which direction. One field only: the
 *  other is not consulted, not even to break a tie — see the backend's
 *  index/queries.py `Order`. Sorting by time ascending (oldest first) is also
 *  bounded to the last 30 days unless an explicit from/to says otherwise. */
export type SortBy = 'time' | 'confidence';
export type SortDir = 'desc' | 'asc';

export interface PersonQuery {
  query: string;
  top_k?: number;
  score_threshold?: number;
  /** A registry slug; the backend rejects one that isn't searchable. */
  camera?: string | null;
  time_from?: string | null;
  time_to?: string | null;
  sort_by?: SortBy;
  sort_dir?: SortDir;
}

export interface VehicleQuery {
  query: string;
  top_k?: number;
  score_threshold?: number;
  plate?: string | null;
  vehicle_type?: string | null;
  color?: string | null;
  /** A registry slug; the backend rejects one that isn't searchable. */
  camera?: string | null;
  /** An explicit range replaces the recent window the search starts from. */
  time_from?: string | null;
  time_to?: string | null;
  sort_by?: SortBy;
  sort_dir?: SortDir;
}

/** Strip empty filters — the API treats `null` as "unset" but `""` as a literal. */
function clean<T extends Record<string, unknown>>(o: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) {
    if (v !== null && v !== undefined && v !== '') out[k] = v;
  }
  return out as Partial<T>;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: 'POST', body: JSON.stringify(body) });
}

export function searchPeople(q: PersonQuery): Promise<SearchResponse<PersonHit>> {
  return post('/search/people', clean({ top_k: 24, score_threshold: 0.05, ...q }));
}

/**
 * Search-by-example: the body is the crop itself (a raw JPEG blob — a box the
 * operator drew on a playback frame), filters ride the query string. Not
 * `post()`: this is the one search whose body is not JSON, end to end — the
 * backend and the index both take the bytes raw, so no multipart parser
 * exists anywhere on the path.
 */
export async function searchPeopleByImage(
  image: Blob,
  opts: { top_k?: number; score_threshold?: number; camera?: string | null;
          time_from?: string | null; time_to?: string | null } = {},
): Promise<SearchResponse<PersonHit>> {
  const params = new URLSearchParams();
  params.set('top_k', String(opts.top_k ?? 24));
  // Image-to-image similarity runs hotter than text-to-image; the backend
  // defaults to 0.15 for the same reason — keep the two in step.
  params.set('score_threshold', String(opts.score_threshold ?? 0.15));
  if (opts.camera) params.set('camera', opts.camera);
  if (opts.time_from) params.set('time_from', opts.time_from);
  if (opts.time_to) params.set('time_to', opts.time_to);
  const resp = await fetch(`/api/search/people/by-image?${params}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { ...(await authHeaders()), 'Content-Type': 'image/jpeg' },
    body: image,
  });
  if (!resp.ok) throw await toApiError(resp);
  return resp.json();
}

// ── faces ───────────────────────────────────────────────────────────────────
// SEARCH, NOT IDENTIFICATION. There is no name anywhere in this path: the
// answer is "stored faces that look like this photo", ranked, scores shown.

export interface FaceHit {
  id: string;
  /** Absent on the gallery listing — nothing has been compared yet. */
  score?: number | null;
  camera: SearchCamera | null;
  camera_id: string;
  when_ms: number;
  tracker_id?: string | null;
  confidence?: number | null;
  /** Face width in source pixels. Explains a weak score honestly. */
  face_width_px?: number | null;
  bbox?: number[] | null;
}

/** What the index made of one uploaded photo, in upload order. */
export interface FacePhotoReport {
  /** 1-based position in the upload. */
  photo: number;
  faces_detected: number;
  /** False when no usable face was found — the photo did not shape the query. */
  used: boolean;
  score?: number | null;
  width_px?: number | null;
}

export interface FaceSearchResponse extends SearchResponse<FaceHit> {
  /** Faces found in the UPLOADED photos. 0 means the query was the problem. */
  faces_detected?: number;
  detail?: string;
  query_face?: { score: number; width_px: number };
  photos?: FacePhotoReport[];
  photos_used?: number;
  /** Lowest similarity between any two query photos; null for one photo. On
   *  the SAME scale as match scores, so the same bands read it. */
  agreement?: number | null;
}

/**
 * Search by photograph.
 *
 * NO DEFAULT THRESHOLD, deliberately, where the person image search defaults to
 * 0.15. Face scores are not on the same scale: measured on this product's own
 * footage, two photos of the SAME person score a median 0.387 and different
 * people 0.117. A floor borrowed from CLIP would hide nearly every true match.
 */
export async function searchFacesByImage(
  images: Blob | Blob[],
  opts: { top_k?: number; score_threshold?: number; camera?: string | null;
          time_from?: string | null; time_to?: string | null;
          min_width_px?: number } = {},
): Promise<FaceSearchResponse> {
  const params = new URLSearchParams();
  params.set('top_k', String(opts.top_k ?? 24));
  params.set('score_threshold', String(opts.score_threshold ?? 0));
  if (opts.camera) params.set('camera', opts.camera);
  if (opts.time_from) params.set('time_from', opts.time_from);
  if (opts.time_to) params.set('time_to', opts.time_to);
  if (opts.min_width_px) params.set('min_width_px', String(opts.min_width_px));
  const list = Array.isArray(images) ? images : [images];
  let send: { headers: Record<string, string>; body: BodyInit };
  if (list.length === 1) {
    // One photo is the raw body — the original contract.
    send = { headers: { ...(await authHeaders()), 'Content-Type': 'image/jpeg' }, body: list[0] };
  } else {
    // Several photos of ONE person, pooled into a single query by the index.
    // No Content-Type: the browser writes the multipart boundary into it, and a
    // hand-set header would drop it.
    const form = new FormData();
    list.forEach((b, i) => form.append('photos', b, `photo-${i + 1}`));
    send = { headers: await authHeaders(), body: form };
  }
  const resp = await fetch(`/api/search/faces/by-image?${params}`, {
    method: 'POST',
    credentials: 'same-origin',
    ...send,
  });
  if (!resp.ok) throw await toApiError(resp);
  return resp.json();
}

/** What the face index holds, newest first — what the tab opens on. */
export async function recentFaces(
  opts: { limit?: number; camera?: string | null; min_width_px?: number } = {},
): Promise<{ results: FaceHit[]; total: number; withheld?: number | null }> {
  const params = new URLSearchParams();
  params.set('limit', String(opts.limit ?? 60));
  if (opts.camera) params.set('camera', opts.camera);
  if (opts.min_width_px) params.set('min_width_px', String(opts.min_width_px));
  const resp = await fetch(`/api/search/faces/recent?${params}`, {
    credentials: 'same-origin',
    headers: await authHeaders(),
  });
  if (!resp.ok) throw await toApiError(resp);
  return resp.json();
}

// NO faceCropUrl HELPER. Crop images come from an authenticated proxy, so a
// URL handed to <img src> answers 401 and renders as an empty tile. Use
// CropThumb (pages/smartsearch/ResultCard), which fetches with the token and
// makes an object URL — that is how People, Vehicles and the dashboard do it.

export function searchVehicles(q: VehicleQuery): Promise<SearchResponse<VehicleHit>> {
  return post('/search/vehicles', clean({ top_k: 24, score_threshold: 0.05, ...q }));
}

/** Cameras a search can return something for — the camera filter's options. */
export function searchableCameras(): Promise<{
  cameras: SearchCamera[];
  /** False when the recorder was unreachable, so the list is registry-only. */
  recorder_available: boolean;
}> {
  return apiFetch('/search/cameras');
}

/** One time a plate was read on one camera. */
export interface PlateSighting {
  id: string;
  camera: SearchCamera;
  when_ms: number;
  vehicle_type: string | null;
  color: string | null;
  confidence: number | null;
}

/** Every sighting of one plate, newest first — the ANPR result shape.
 *  Grouped rather than ranked: a plate is an exact identifier, so the useful
 *  question is "where has this vehicle been", not "which crop looks most like
 *  the description". */
export interface PlateGroup {
  plate: string;
  sightings: PlateSighting[];
  count: number;
  first_seen_ms: number;
  last_seen_ms: number;
  cameras: string[];
}

export interface PlateSearchResult {
  plates: PlateGroup[];
  sightings: number;
  cameras: number;
  /** False = the index cannot read plates on this deployment (no localiser
   *  weights), which is why there are no results. Null = it did not say.
   *  Distinct from "this plate was never seen", and the UI must not conflate
   *  the two. */
  plates_active: boolean | null;
  withheld?: Record<string, number>;
}

export function searchPlates(body: {
  plate: string; camera?: string | null;
  time_from?: string | null; time_to?: string | null; limit?: number;
}): Promise<PlateSearchResult> {
  return apiFetch('/search/plates', { method: 'POST', body: JSON.stringify(body) });
}

export interface DomainStats {
  /** Entries belonging to this VMS's cameras — not the whole index, which is
   *  shared with other deployments. Null when the index ignored the camera
   *  filter (an older build), i.e. the count is unknown rather than zero. */
  vectors_count: number | null;
  scoped: boolean;
  status: string;
}

/** How much of the index belongs to this VMS's cameras, and whether the index
 *  answers at all. Never throws for an unreachable index — that is reported as
 *  `reachable: false`. */
export function indexStats(): Promise<{
  reachable: boolean;
  /** False when no index endpoint is deployed at all. Distinct from
   *  `reachable: false`, which means one is deployed and not answering —
   *  an operator told "unreachable" goes looking for a broken service. */
  configured?: boolean;
  /** Cameras the count covers: searchable, registered, known to the recorder. */
  cameras: number;
  domains: Partial<Record<'people' | 'vehicles', DomainStats>>;
  detail?: string;
}> {
  return apiFetch('/search/stats');
}
