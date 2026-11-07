/**
 * People Distribution — zone occupancy, live and by day.
 *
 * NUMBERS, NOT PICTURES. The heatmap is an image and needs an object store;
 * this is a handful of integers per camera and lives in Postgres. Nothing here
 * fetches a blob.
 *
 * `live` is decided by the API, not here: one sample cadence, one staleness
 * rule, so every client draws the same line between "now" and "last known".
 */
import { apiFetch } from '@/lib/api';

export interface ZoneCount {
  zone_id: string;
  zone_name: string;
  current_count: number;
  dwell_seconds_max: number;
  dwell_seconds_avg: number;
}

export interface Sample {
  captured_at: string;
  age_seconds: number;
  total_count: number;
  zones: ZoneCount[];
  source: string;
  /** Fresh enough to describe the present, as the API judges it. */
  live: boolean;
}

export interface DistributionCamera {
  camera_id: string;
  slug: string;
  name: string;
  camera_enabled: boolean;
  health: string | null;
  /** null = configured, but nothing has reported yet. Not the same as empty. */
  sample: Sample | null;
}

export interface LiveOverview {
  cameras: DistributionCamera[];
  stale_after_seconds: number;
  engine: string;
}

export interface DaySummaryZone {
  zone_name: string;
  peak_count: number;
  avg_count: number;
}

export interface HourZone {
  zone_name: string;
  /** DISTINCT people seen in this zone during the hour — not detections. */
  unique_people: number;
}

export interface HourBucket {
  hour_start: string;
  /** "06:00–07:00" — a fixed clock hour, never a rolling window. */
  hour_label: string;
  zones: HourZone[];
  /** Distinct people anywhere on the camera this hour: the UNION across zones,
   *  not the sum. Someone who crosses Zone 1 → Zone 2 is one person. */
  total_people: number;
}

export interface DayReading {
  camera_id: string;
  slug: string;
  name: string;
  day: string;
  hours: HourBucket[];
  series: Sample[];
  summary: { samples: number; peak_total: number; zones: DaySummaryZone[] };
}

export function fetchDistributionLive(): Promise<LiveOverview> {
  return apiFetch<LiveOverview>('/analytics/people-distribution');
}

export function fetchDistributionDay(cameraId: string, day: string) {
  return apiFetch<DayReading>(
    `/analytics/people-distribution/${encodeURIComponent(cameraId)}/day`
    + `?day=${encodeURIComponent(day)}`);
}

/** "12 seconds ago" — the question a live panel is actually answering. */
export function agoText(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} second${s === 1 ? '' : 's'} ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} minute${m === 1 ? '' : 's'} ago`;
  const h = Math.round(m / 60);
  return `${h} hour${h === 1 ? '' : 's'} ago`;
}

/** Today as the API wants a day: the LOCAL calendar date, not UTC. */
export function localDay(d: Date = new Date()): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-`
    + `${String(d.getDate()).padStart(2, '0')}`;
}

/** One fixed slice of a day, as the charts draw it. Nulls = no samples (not reporting). */
export interface Bucket {
  start: string;
  label: string;
  end_label: string;
  samples: number;
  /** Mean number of people in the zones at once over the slice. */
  people_avg: number | null;
  /** Most people in the zones at the same moment. */
  people_peak: number | null;
  /** Mean time the people present had already been there. */
  dwell_avg_s: number | null;
  /** The longest single stay seen in the slice. */
  dwell_max_s: number | null;
}

export interface BucketReading {
  camera_id: string;
  name: string;
  day: string;
  minutes: number;
  buckets: Bucket[];
}

/** The chart intervals offered, smallest first. */
export const INTERVALS: readonly { minutes: number; label: string }[] = [
  { minutes: 15, label: '15 min' }, { minutes: 30, label: '30 min' },
  { minutes: 60, label: '1 hour' }, { minutes: 180, label: '3 hours' },
  { minutes: 360, label: '6 hours' }, { minutes: 720, label: '12 hours' },
  { minutes: 1440, label: '24 hours' },
];

export function fetchDistributionBuckets(cameraId: string, day: string, minutes: number) {
  return apiFetch<BucketReading>(
    `/analytics/people-distribution/${encodeURIComponent(cameraId)}/buckets`
    + `?day=${encodeURIComponent(day)}&minutes=${minutes}`);
}

/** "45s", "3m 20s", "1h 05m" — a stay, read at a glance. */
export function durationText(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}
