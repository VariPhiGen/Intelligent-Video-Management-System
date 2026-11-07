/**
 * types.ts — the Data Collection wire contracts and the two formatters the
 * whole page leans on.
 *
 * `interval_seconds` arrives FROM THE SERVER and is never recomputed here. It
 * is derived where the sampling happens (backend models.dataset_interval_seconds,
 * the same inputs dataset_build.plan_timestamps uses), so recomputing it in the
 * browser would eventually disagree with the frames actually produced.
 */

export type DatasetStatus = 'draft' | 'collecting' | 'ready' | 'failed' | 'cancelled';

export interface Dataset {
  id: number;
  name: string;
  description: string | null;
  status: DatasetStatus;
  camera_slug: string;
  window_start: string;
  window_end: string;
  target_frames: number;
  jpeg_quality: number;
  /** Server-derived sampling interval. Display it; never compute it. */
  interval_seconds: number | null;
  frames_collected: number;
  frames_missing: number;
  /** Detection filter: set when only frames containing target_classes are kept. */
  model_id: string | null;
  model_name: string | null;
  target_classes: string[];
  min_confidence: number | null;
  scan_interval_seconds: number | null;
  frames_scanned: number;
  /** PTZ camera: frames only while it was confirmed at these presets (VMS ids). Empty = the whole window. */
  ptz_preset_ids: string[];
  frames_dir: string | null;
  bytes_total: number | null;
  build_error: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  labelstudio_project_id: number | null;
  labelstudio_task_count: number | null;
  labelstudio_exported_at: string | null;
  s3_bucket: string | null;
  s3_prefix: string | null;
  s3_uploaded_count: number | null;
  s3_uploaded_at: string | null;
}

export interface DetectionModel {
  id: string;
  name: string;
  filename: string;
  format: 'pt' | 'onnx';
  bytes: number;
  classes: string[];
  created_at: number;
}

export interface Frame {
  filename: string;
  timestamp: string;
  sha256: string;
  bytes: number;
  /** A PTZ dataset at presets: the preset the frame shows, and how its view was told ("picture" | "position"). */
  preset_id?: string;
  preset_name?: string;
  view?: string;
}

/** GET /datasets/ptz-time — a PTZ camera's presets and its time at each inside the window. */
export interface PtzTime {
  ptz: boolean;
  source: '' | 'picture' | 'position';
  window_seconds?: number;
  presets: { preset_id: string; token: string; name: string; seconds: number; visits: number }[];
}

/** "1 h 05 min" / "4 min 10 s" / "12 s". */
export function fmtSpan(seconds: number): string {
  const s = Math.round(seconds);
  if (s >= 3600) return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')} min`;
  if (s >= 60) return `${Math.floor(s / 60)} min ${s % 60} s`;
  return `${s} s`;
}

export interface FrameManifest {
  dataset_id: number;
  frames_dir: string | null;
  collected: number;
  missing: number;
  bytes_total: number | null;
  interval_seconds: number | null;
  frames: Frame[];
}

export interface Coverage {
  known: boolean;
  covered: boolean;
  earliest: string | null;
  latest: string | null;
  message: string;
}

export interface ExportConfig {
  configured: boolean;
  url: string;
  default_project_id: number | null;
  s3_credentials: boolean;
  s3_region: string;
  default_bucket: string;
  default_prefix: string;
}

export interface ExportOutcome {
  ok: boolean;
  error?: string;
  tasks?: number;
  project_id?: number;
  uploaded?: number;
  skipped?: number;
  /** Frames the manifest lists but the dataset directory no longer holds.
   *  Not sent anywhere; re-collecting is the only remedy. */
  missing_on_disk?: number;
  /** Label Studio: files actually sent, and a few missing names to look for. */
  sent?: number;
  missing_examples?: string[];
  bucket?: string;
  prefix?: string;
}

/**
 * Measured cost of one NVR snapshot, in seconds.
 *
 * The NVR decodes from the segment start deliberately — input seeking returns
 * silently wrong frames on MPEG-TS — which its own source measures at 0.18 s
 * one second into a segment and 2.58 s fifty-five seconds in. Frames land
 * uniformly across segments, so ~1.3 s is the mean. Divided by the sampler's
 * concurrency of 4, this is what lets the form warn an operator BEFORE they
 * ask for something that runs for two hours.
 */
const SECONDS_PER_FRAME = 1.3;
const SAMPLER_CONCURRENCY = 4;

export function estimateSeconds(targetFrames: number): number {
  return (targetFrames * SECONDS_PER_FRAME) / SAMPLER_CONCURRENCY;
}

/** "about 2 min", "about 1 h 50 min" — an estimate, phrased as one. */
export function fmtEstimate(targetFrames: number): string {
  const s = estimateSeconds(targetFrames);
  if (s < 90) return `about ${Math.max(1, Math.round(s))} s`;
  const m = Math.round(s / 60);
  if (m < 90) return `about ${m} min`;
  const h = Math.floor(m / 60);
  return `about ${h} h ${m % 60} min`;
}

/**
 * The sampling interval, spelled out. THIS IS THE NUMBER THAT MATTERS to
 * whoever will train on these images — "200 frames" says nothing about density,
 * "one every 36 s" says everything.
 */
export function fmtInterval(seconds: number | null): string {
  if (seconds === null || seconds === undefined || !isFinite(seconds)) return '—';
  if (seconds < 1) return `one every ${seconds.toFixed(2)} s`;
  if (seconds < 90) return `one every ${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
  const m = seconds / 60;
  if (m < 90) return `one every ${m < 10 ? m.toFixed(1) : Math.round(m)} min`;
  return `one every ${(m / 60).toFixed(1)} h`;
}

export function statusBadge(status: DatasetStatus): { cls: string; label: string } {
  switch (status) {
    case 'ready':      return { cls: 'badge-green', label: 'Ready' };
    case 'collecting': return { cls: 'badge-blue', label: 'Collecting' };
    case 'failed':     return { cls: 'badge-red', label: 'Failed' };
    case 'cancelled':  return { cls: 'badge-gray', label: 'Cancelled' };
    default:           return { cls: 'badge-gray', label: 'Draft' };
  }
}
