/**
 * heatmaps.ts — heat overlays: the API client for the Heatmap Overlay page.
 *
 * A heatmap is accumulated by the analytics service for as long as a camera's
 * session runs, rendered about once a minute and stored by the API. The browser
 * NEVER drives that work: opening this page starts nothing, closing it stops
 * nothing, and refreshing only asks for the latest artifact that already exists.
 *
 * Two switches decide whether a camera accumulates, and they are not the same:
 * the AI Config SCHEDULE (edited in Zones & Analytics) says when it is allowed
 * to run, and the RUNTIME switch here says whether the operator wants it
 * running now. Turning it off never changes the schedule.
 */
import { apiFetch } from './api';

/** Where a session stands. `frozen` can be resumed; `closed` cannot. */
export type HeatmapStatus = 'active' | 'frozen' | 'closed';

export interface HeatmapSession {
  id: string;
  status: HeatmapStatus;
  started_at: string;
  ended_at: string | null;
  close_reason: string | null;
  points: number;
  frames: number;
  params: Record<string, unknown>;
}

export interface HeatmapArtifact {
  id: number;
  session_id: string;
  kind: 'live' | 'snapshot' | 'final';
  captured_at: string;
  period_start: string;
  width: number;
  height: number;
}

export interface HeatmapCamera {
  camera_id: string;
  slug: string;
  name: string;
  camera_enabled: boolean;
  health: string | null;
  /** The operator's switch, not the schedule. */
  runtime_enabled: boolean;
  schedule: { active_days: string[] | null; active_hours: { start: string; end: string } | null } | null;
  session: HeatmapSession | null;
  latest_artifact: HeatmapArtifact | null;
}

export interface HeatmapOverview {
  cameras: HeatmapCamera[];
  /** How often the backend renders a new picture, in seconds. */
  snapshot_interval_s: number;
}

/** What the card shows as its state, which is not quite the session's. */
export type CardState = 'on' | 'frozen' | 'off' | 'idle' | 'waiting';

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/**
 * Is this camera inside its configured heatmap window right now?
 *
 * Evaluated on the BROWSER's clock against the operator's own schedule, which
 * is the same wall clock the appliance uses (the API serves local days and
 * local snapshot boundaries). `active_hours: null` means all day, and an empty
 * or missing `active_days` means every day -- both are how the backend spells
 * "no restriction", and reading either as "restricted" is what produced the
 * bug this function exists to prevent.
 */
export function withinSchedule(cam: HeatmapCamera, now: Date = new Date()): boolean {
  const s = cam.schedule;
  if (!s) return true;
  if (s.active_days?.length && !s.active_days.includes(DAYS[now.getDay()])) return false;
  const h = s.active_hours;
  if (!h?.start || !h?.end) return true;                 // all day
  const clock = now.toTimeString().slice(0, 8);          // "HH:MM:SS"
  return h.start <= h.end
    ? clock >= h.start && clock <= h.end
    : clock >= h.start || clock <= h.end;                // window spans midnight
}

/**
 * IDLE IS NOT THE SAME AS OUT OF SCHEDULE. Both mean "no active session", but
 * only one of them is the schedule's doing, and the card used to assert the
 * schedule without ever consulting it -- a camera added minutes ago on a 24x7
 * schedule was told it was "outside its schedule" when the truth was that no
 * frame had arrived yet. `waiting` is that case: in its window, nothing yet.
 */
export function cardState(cam: HeatmapCamera, now: Date = new Date()): CardState {
  if (!cam.runtime_enabled) return cam.session ? 'frozen' : 'off';
  if (cam.session?.status === 'active') return 'on';
  return withinSchedule(cam, now) ? 'waiting' : 'idle';
}

export function fetchHeatmaps(): Promise<HeatmapOverview> {
  return apiFetch<HeatmapOverview>('/analytics/heatmaps');
}

/** What the heatmap of a range is made of. Database only, cheap to poll. */
export interface HeatmapRange {
  camera_id: string;
  name: string;
  start: string;
  end: string;
  /** Stored 5-minute blocks that overlap the range. */
  blocks: number;
  /** The block analytics is still filling was added (the range ends now). */
  includes_live: boolean;
  /** Heat points (person detections) in those blocks. */
  points: number;
  /** The period the blocks actually cover — ranges are answered in whole blocks. */
  covered_from: string | null;
  covered_to: string | null;
  /** When the background frame was taken: an ISO time, "live", or null (grey). */
  background_at: string | null;
}

function rangeQuery(start: Date, end: Date): string {
  return new URLSearchParams({ start: start.toISOString(), end: end.toISOString() }).toString();
}

export function fetchHeatmapRange(cameraId: string, start: Date, end: Date) {
  return apiFetch<HeatmapRange>(
    `/analytics/heatmaps/${encodeURIComponent(cameraId)}/range?${rangeQuery(start, end)}`);
}

/** What is stored for one camera: which local days have heat, from when, and
 *  the storage caps (set on the Heatmap overlay activity in AI Config). */
export interface HeatmapAvailability {
  camera_id: string;
  /** Local calendar days ("2026-10-14") with at least one stored block. */
  days: string[];
  first_at: string | null;
  last_at: string | null;
  blocks: number;
  used_bytes: number;
  retention_days: number;
  storage_mb: number;
}

export function fetchHeatmapAvailability(cameraId: string) {
  return apiFetch<HeatmapAvailability>(
    `/analytics/heatmaps/${encodeURIComponent(cameraId)}/availability`);
}

/** "20261014-0600", on the browser's clock, for file names. */
function stamp(d: Date): string {
  return toInput(d).replace(/[-:]/g, '').replace('T', '-');
}

/** heatmap_<camera>_<from>_to_<to>.jpg -- what a downloaded picture is called. */
export function heatmapFileName(camera: string, start: Date, end: Date): string {
  const name = camera.trim().replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'camera';
  return `heatmap_${name}_${stamp(start)}_to_${stamp(end)}.jpg`;
}

/** Save a picture already in the browser (an object URL) as a local file. */
export function saveObjectUrl(url: string, fileName: string): void {
  const a = document.createElement('a');
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** How often a range that ends "now" is redrawn. */
export const RANGE_REFRESH_MS = 60_000;

/** "2026-10-13T09:05" for a datetime-local input, on the browser's clock. */
export function toInput(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function startOfToday(now: Date = new Date()): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

/** The summed heat of [start, end) over a recent frame. Fetch with `apiBlob`.
 *  Always relative: the hottest spot in the range is red, so a long range does
 *  not turn solid red. */
export function heatmapRangeImagePath(cameraId: string, start: Date, end: Date): string {
  return `/analytics/heatmaps/${encodeURIComponent(cameraId)}/range/image?`
    + rangeQuery(start, end);
}

/**
 * Turn one camera's heatmap on or off.
 *
 * `mode` only matters when turning ON with a frozen session waiting: "resume"
 * continues it and keeps its heat, "new" closes it and starts a fresh one.
 */
export function setHeatmapRuntime(cameraId: string, enabled: boolean,
                                  opts: { mode?: 'resume' | 'new'; reason?: string } = {}) {
  return apiFetch<{ camera: string; runtime_enabled: boolean; mode: string }>(
    `/analytics/heatmaps/${encodeURIComponent(cameraId)}/runtime`,
    { method: 'POST', body: JSON.stringify({ enabled, mode: opts.mode || 'resume',
                                             reason: opts.reason || null }) });
}
