/**
 * aiEvents.ts — AI activity events: the API client and the playback hand-off.
 *
 * An event is what a camera's CONFIGURED activity decided happened, as reported
 * by the analytics pipeline and stored by the API (services/ai_events.py). The
 * browser never derives events itself — no detections are turned into events
 * here — it only reads them.
 */
import { apiFetch } from './api';

export interface AiEventActivity {
  key: string;
  /** Operator-facing name from the activity catalog. */
  label: string;
  color: string;
}

export interface AiEvent {
  id: string;
  camera: { id: string; slug: string; name: string };
  activity: AiEventActivity;
  /** The region's name as drawn in the zone editor; null = whole frame. */
  zone: string | null;
  started_at: string;
  /** null for a point event, or one still open. */
  ended_at: string | null;
  duration_s: number | null;
  confidence: number | null;
  track_id: string | null;
  object_class: string | null;
  attributes: Record<string, unknown>;
  source: string;
  /** Epoch seconds. `start` already includes the API's pre-roll. */
  playback: { camera: string; start: number; end: number };
}

export interface CameraEventsCard {
  camera: { id: string; slug: string; name: string; enabled: boolean };
  /** Every activity configured on the camera, in configuration order. */
  activities: AiEventActivity[];
  /** The camera's most recent events, newest first. */
  events: AiEvent[];
  last_event_at: string | null;
}

export interface EventsOverview {
  generated_at: string;
  ticker_limit: number;
  /** Latest events across every camera, newest first, at most `ticker_limit`. */
  latest: AiEvent[];
  /** One card per camera with at least one configured activity. */
  cameras: CameraEventsCard[];
}

export interface EventsPage {
  events: AiEvent[];
  /** Pass back as `before` for the next (older) page; null on the last. */
  next_before: string | null;
}

export interface EventQuery {
  camera?: string;
  activity?: string;
  /** ISO 8601 with a timezone. */
  from?: string;
  to?: string;
  before?: string | null;
  limit?: number;
}

/** The API enforces the same cap; mirrored so the UI never shows more. */
export const TICKER_LIMIT = 10;

/** Seconds of footage before the moment an event was raised. */
export const EVENT_CLIP_BEFORE_S = 10;
/** Seconds of footage from that moment on: the moment itself and what follows. */
export const EVENT_CLIP_AFTER_S = 30;
/** How far past the end of an event that reports one the clip runs, when it can. */
const EVENT_CLIP_PAST_END_S = 20;
/** No event clip is longer than this, whatever the event's own duration. */
export const EVENT_CLIP_MAX_SECONDS = 50;

/** What the clip popup needs to play one event. */
export interface EventClip {
  cam: { slug: string; name: string };
  /** When the event started — the clip's anchor. */
  whenMs: number;
  before: number;
  after: number;
  label: string;
}

/**
 * Open Playback: a short clip of the event, played in place in the same popup
 * Smart Search uses for a hit — never a trip to the Playback page.
 *
 * An event is a moment (a vehicle reached its parking duration, a person walked
 * into a restricted zone), so the clip is that moment in context: 10 s before it and 30 s
 * from it — 40 s. An event that reports a duration may run on to 20 s past its
 * end, but no clip is ever longer than 50 s: the full stretch of footage belongs
 * on the Playback page, not in this popup.
 */
export function eventClip(
  ev: Pick<AiEvent, 'camera' | 'activity' | 'started_at' | 'duration_s' | 'playback'>,
): EventClip {
  const startedS = Date.parse(ev.started_at) / 1000;
  const lasted = typeof ev.duration_s === 'number' && ev.duration_s > 0 ? Math.ceil(ev.duration_s) : 0;
  const before = EVENT_CLIP_BEFORE_S;
  const after = Math.min(Math.max(EVENT_CLIP_AFTER_S, lasted + EVENT_CLIP_PAST_END_S),
                         EVENT_CLIP_MAX_SECONDS - before);
  return {
    cam: { slug: ev.playback.camera || ev.camera.slug, name: ev.camera.name },
    whenMs: startedS * 1000,
    before,
    after,
    label: ev.activity.label,
  };
}

/** A `datetime-local` value (browser-local wall time) as ISO UTC, or undefined. */
export function localInputToIso(value: string): string | undefined {
  if (!value) return undefined;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

export function eventsQueryString(q: EventQuery): string {
  const p = new URLSearchParams();
  if (q.camera) p.set('camera', q.camera);
  if (q.activity) p.set('activity', q.activity);
  if (q.from) p.set('from', q.from);
  if (q.to) p.set('to', q.to);
  if (q.before) p.set('before', q.before);
  if (q.limit) p.set('limit', String(q.limit));
  const s = p.toString();
  return s ? `?${s}` : '';
}

export function fetchEventsOverview(perCamera = 5): Promise<EventsOverview> {
  return apiFetch<EventsOverview>(`/analytics/events/overview?per_camera=${perCamera}`);
}

export function fetchEvents(q: EventQuery): Promise<EventsPage> {
  return apiFetch<EventsPage>(`/analytics/events${eventsQueryString(q)}`);
}

// ── Entry / Exit ─────────────────────────────────────────────────────────────

/** The activity whose events are tripwire crossings. Each one is an Entry or an
 *  Exit, counted on the Events page's graph; there is no clip to play for one. */
export const ENTRY_EXIT_ACTIVITY = 'entry_exit_WLE_logs';

export type CrossingDirection = 'entry' | 'exit';

export function isEntryExit(ev: Pick<AiEvent, 'activity'>): boolean {
  return ev.activity.key === ENTRY_EXIT_ACTIVITY;
}

/** Which way an Entry / Exit event crossed; null for any other event, and for the
 *  tripwire touches recorded before crossings had a direction (kept, never counted). */
export function crossingDirection(ev: Pick<AiEvent, 'activity' | 'attributes'>): CrossingDirection | null {
  if (!isEntryExit(ev)) return null;
  const d = ev.attributes?.direction;
  return d === 'entry' || d === 'exit' ? d : null;
}

/** The graph's ranges — exactly the API's (services/camera-mgmt/backend/services/entry_exit.py). */
export const ENTRY_EXIT_RANGES: readonly { minutes: number; label: string }[] = [
  { minutes: 5, label: '5 min' }, { minutes: 15, label: '15 min' }, { minutes: 30, label: '30 min' },
  { minutes: 60, label: '1 h' }, { minutes: 120, label: '2 h' }, { minutes: 360, label: '6 h' },
  { minutes: 720, label: '12 h' }, { minutes: 1440, label: '24 h' },
];
export const DEFAULT_ENTRY_EXIT_MINUTES = 60;

export interface EntryExitTripwire {
  /** The region id. */
  id: string;
  name: string;
  direction: string | null;
  /** false: no entry direction is set, so this tripwire's crossings are not counted. */
  oriented: boolean;
}

export interface EntryExitBucket {
  /** ISO UTC start of the bucket. */
  start: string;
  entry: number;
  exit: number;
}

export interface EntryExitStats {
  camera: { id: string; slug: string; name: string };
  /** Is Entry / Exit configured on this camera now? */
  configured: boolean;
  tripwires: EntryExitTripwire[];
  /** The one tripwire counted, or null for all of them. */
  tripwire: string | null;
  minutes: number;
  bucket_seconds: number;
  /** Minutes east of UTC of the clock the buckets start on round times of. */
  tz_offset: number;
  start: string;
  end: string;
  /** The sum of the buckets. */
  totals: { entry: number; exit: number };
  buckets: EntryExitBucket[];
}

/** Minutes east of UTC, so the API's buckets start on round times of this browser's clock. */
export function localUtcOffsetMinutes(at: Date = new Date()): number {
  return -at.getTimezoneOffset() || 0;
}

export function entryExitQueryString(camera: string, minutes: number, tripwire?: string | null,
                                     tzOffset = localUtcOffsetMinutes()): string {
  const p = new URLSearchParams({ camera, minutes: String(minutes) });
  if (tripwire) p.set('tripwire', tripwire);
  p.set('tz_offset', String(tzOffset));
  return `?${p.toString()}`;
}

/** One camera's Entry and Exit crossings per time bucket, counted by the API. */
export function fetchEntryExit(camera: string, minutes: number, tripwire?: string | null): Promise<EntryExitStats> {
  return apiFetch<EntryExitStats>(`/analytics/events/entry-exit${entryExitQueryString(camera, minutes, tripwire)}`);
}
