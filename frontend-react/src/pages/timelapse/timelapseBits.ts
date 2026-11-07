/**
 * timelapseBits.ts — the pure half of the time-lapse UI: the settings shape the
 * backend validates (services/timelapse.TimelapseConfig), the choices offered,
 * and how sizes and schedules are spelled. Kept apart from the components so the
 * parts that must agree with the backend are reachable by a test.
 */

export interface TimelapseRule { days: number[]; start: string; end: string }
export interface TimelapseSchedule { mode: 'weekly' | 'monthly'; rules: TimelapseRule[] }

export interface TimelapseConfig {
  interval_s: number;
  schedule: TimelapseSchedule | null;
  skip_dark: boolean;
  dark_threshold: number;
  skip_unchanged: boolean;
  unchanged_threshold: number;
  ptz_preset_only: string | null;
  ptz_tolerance: number;
  resolution: 'original' | '1080p' | '720p';
  jpeg_quality: number;
  retention_days: number;
  max_storage_mb: number;
  auto_render_daily: boolean;
  render_fps: 24 | 30;
  render_overlay: boolean;
  render_deflicker: boolean;
}

export interface TimelapseEstimate {
  frames_per_day: number;
  mb_per_day: number;
  mb_total: number;
  cap_reached_after_days: number | null;
  video_seconds_per_day: number;
}

export interface TimelapseStatus { result: string; at: string | null; detail: string }

export interface TimelapseCameraDetail {
  id: string; name: string; slug: string; enabled: boolean;
  config: TimelapseConfig; estimate: TimelapseEstimate;
  frames: number; bytes: number; last_captured_at: string | null;
  status: TimelapseStatus | null; privacy_masks: number; ptz_capable: boolean; object_store: boolean;
  /** The site defaults this camera follows, and the settings it changes on top of them. */
  defaults?: TimelapseConfig; changed?: (keyof TimelapseConfig)[];
  /** Set when the disk guard switched this camera off. */
  paused?: TimelapsePause | null;
}

// ── Site defaults + disk guard ───────────────────────────────────────────────

export interface TimelapsePause { at: string; reason: string; camera_ids: string[] }
export interface TimelapseGuard { floor_free_pct: number; budget_pct: number }
export interface TimelapseDisk extends TimelapseGuard {
  total_bytes: number; free_bytes: number; timelapse_bytes: number; frames_bytes: number; renders_bytes: number;
  free_pct?: number; budget_bytes?: number; over: boolean; reason: string;
}
export interface TimelapseDefaults {
  config: TimelapseConfig; builtin: TimelapseConfig; estimate: TimelapseEstimate; bulk_max: number;
  per_camera_only: string[]; site_only_excludes: string[]; guard: TimelapseGuard; disk: TimelapseDisk;
  paused: TimelapsePause | null; enabled_cameras: number;
}
export interface ChangedCamera {
  id: string; name: string; slug: string; enabled: boolean;
  changes: Record<string, { value: unknown; default: unknown }>;
}

/** Plain names for the settings, for the "changed per camera" list. */
export const SETTING_LABEL: Record<keyof TimelapseConfig, string> = {
  interval_s: 'Capture', schedule: 'When', skip_dark: 'Skip dark stills', dark_threshold: 'Dark threshold',
  skip_unchanged: 'Skip unchanged stills', unchanged_threshold: 'Unchanged threshold',
  ptz_preset_only: 'PTZ preset', ptz_tolerance: 'PTZ tolerance', resolution: 'Resolution', jpeg_quality: 'JPEG quality',
  retention_days: 'Keep stills for', max_storage_mb: 'Size cap', auto_render_daily: 'Video of each day',
  render_fps: 'Frame rate', render_overlay: 'Name and time stamp', render_deflicker: 'Smooth flicker',
};

export function intervalLabel(s: number): string {
  const hit = INTERVALS.find(([v]) => v === s);
  return hit ? hit[1].replace(' (default)', '') : `Every ${s} s`;
}

/** One setting's value, spelled the way the settings form spells it. */
export function settingText(key: string, v: unknown): string {
  if (v === null || v === undefined) return key === 'schedule' ? 'Around the clock' : key === 'ptz_preset_only' ? 'Any position' : '—';
  switch (key) {
    case 'interval_s': return intervalLabel(Number(v));
    case 'schedule': return scheduleLabel(v as TimelapseSchedule);
    case 'retention_days': return `${v} days`;
    case 'max_storage_mb': return fmtMb(Number(v));
    case 'render_fps': return `${v} fps`;
    case 'resolution': return v === 'original' ? "Camera's own" : String(v);
    default: return typeof v === 'boolean' ? (v ? 'On' : 'Off') : String(v);
  }
}

/** Does this camera's value differ from the default? (schedules compared by value) */
export function differs(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) !== JSON.stringify(b);
}

/** "Server: 140 GB free of 500 GB · time-lapse 12.0 GB of its 100 GB budget". */
export function diskLine(d: TimelapseDisk): string {
  if (!d.total_bytes) return "Couldn't measure the server disk";
  const budget = d.budget_bytes ?? d.total_bytes * d.budget_pct / 100;
  return `Server disk: ${fmtBytes(d.free_bytes)} free of ${fmtBytes(d.total_bytes)} · time-lapse uses `
    + `${fmtBytes(d.timelapse_bytes)} of its ${fmtBytes(budget)} budget`;
}

/** Worst case for `n` cameras at these settings, against what the budget has left. */
export function fitsBudget(perCameraMb: number, n: number, d: TimelapseDisk): { needBytes: number; leftBytes: number; fits: boolean } {
  const needBytes = perCameraMb * 1024 * 1024 * n;
  const budget = d.budget_bytes ?? d.total_bytes * d.budget_pct / 100;
  const leftBytes = Math.max(0, Math.min(budget - d.timelapse_bytes, d.free_bytes - d.total_bytes * d.floor_free_pct / 100));
  return { needBytes, leftBytes, fits: !d.total_bytes || needBytes <= leftBytes };
}

export interface TimelapseRender {
  id: string; camera_id: string; kind: 'manual' | 'daily';
  range_start: string; range_end: string; fps: number; overlay: boolean; deflicker: boolean;
  status: 'queued' | 'running' | 'done' | 'failed'; progress: number; frames: number; bytes: number;
  encoder: string | null; error: string | null; requested_by: string | null;
  created_at: string | null; finished_at: string | null; expires_at: string | null; duration_s: number | null;
}

/** The intervals offered. 10 s is the backend's floor; 24 h its ceiling. */
export const INTERVALS: [number, string][] = [
  [10, 'Every 10 seconds'], [30, 'Every 30 seconds'], [60, 'Every minute'], [120, 'Every 2 minutes'],
  [300, 'Every 5 minutes (default)'], [600, 'Every 10 minutes'], [900, 'Every 15 minutes'],
  [1800, 'Every 30 minutes'], [3600, 'Every hour'], [10800, 'Every 3 hours'], [21600, 'Every 6 hours'],
  [86400, 'Once a day'],
];

/** ISO weekday index (0 = Monday), matching services/schedule.py. */
export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** "One weekly rule" is the only shape the tab edits directly; anything richer
 *  (several rules, a monthly calendar) is shown and preserved, not flattened. */
export function isSimpleSchedule(s: TimelapseSchedule | null): boolean {
  return s === null || (s.mode === 'weekly' && s.rules.length === 1);
}

export function scheduleLabel(s: TimelapseSchedule | null): string {
  if (!s) return 'Around the clock';
  if (!isSimpleSchedule(s)) return `Custom schedule (${s.rules.length} rule${s.rules.length === 1 ? '' : 's'}, ${s.mode})`;
  const r = s.rules[0];
  const days = [...r.days].sort((a, b) => a - b);
  const contiguous = days.every((d, i) => i === 0 || d === days[i - 1] + 1);
  const dayText = days.length === 7 ? 'every day'
    : contiguous && days.length > 1 ? `${WEEKDAYS[days[0]]}–${WEEKDAYS[days[days.length - 1]]}`
    : days.map(d => WEEKDAYS[d]).join(', ');
  return `${r.start}–${r.end}, ${dayText}`;
}

export function fmtBytes(n: number | null | undefined): string {
  const v = Number(n || 0);
  if (v < 1024) return `${v} B`;
  if (v < 1024 ** 2) return `${(v / 1024).toFixed(0)} KB`;
  if (v < 1024 ** 3) return `${(v / 1024 ** 2).toFixed(1)} MB`;
  return `${(v / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtMb(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

/** One line the operator reads before switching on. */
export function estimateLine(e: TimelapseEstimate, retentionDays: number): string {
  const parts = [`≈ ${e.frames_per_day} stills a day`, `${fmtMb(e.mb_per_day)} a day`,
                 `${e.video_seconds_per_day} s of video per day`];
  const total = e.cap_reached_after_days
    ? `${fmtMb(e.mb_total)} once the size cap is reached after ${e.cap_reached_after_days} days`
    : `${fmtMb(e.mb_total)} kept over ${retentionDays} days`;
  return `${parts.join(' · ')} · ${total}`;
}

/** Why the newest still may be old — the capture loop's own last outcome. */
export interface PtzPreset { token: string; name: string | null }

/** Picker options for "framed on preset": the camera's presets, plus the saved
 *  value when the camera no longer lists it (or it was typed as a name before the
 *  picker existed) — so opening the tab never silently changes the setting. */
export function presetOptions(presets: PtzPreset[] | null, saved: string | null): { value: string; label: string }[] {
  // null = not read (a viewer, or still loading): show the saved value as-is, no verdict on it.
  const list = presets ?? [];
  const opts = list.map(p => ({ value: p.token, label: p.name ? `${p.name} (${p.token})` : p.token }));
  if (saved && !list.some(p => p.token === saved)) {
    opts.unshift({ value: saved, label: presets === null ? saved : `${saved} (saved; not listed by the camera)` });
  }
  return opts;
}

export function statusText(s: TimelapseStatus | null): string {
  if (!s) return 'No capture attempted yet';
  const words: Record<string, string> = {
    captured: 'Last still captured', dark: 'Skipped: too dark', unchanged: 'Skipped: nothing changed',
    ptz_steering: 'Skipped: an operator is moving the PTZ', off_preset: 'Skipped: camera not at its preset',
    offline: 'No frame: stream offline',
    error: 'Capture failed', busy: 'Waiting for the camera',
  };
  return words[s.result] || s.result;
}

/** Up to `n` evenly spread items, first and last kept — the filmstrip. */
export function evenly<T>(items: T[], n: number): T[] {
  if (items.length <= n) return items;
  const step = (items.length - 1) / (n - 1);
  return Array.from({ length: n }, (_, i) => items[Math.round(i * step)]);
}

// ── Bulk switch (Inventory → bulk bar) ───────────────────────────────────────

/** One camera's line in POST /timelapse/bulk's answer. */
export interface BulkRow {
  camera_id: string; name?: string; ok: boolean;
  result?: 'enabled' | 'updated' | 'disabled' | 'already_enabled' | 'already_disabled';
  error?: string; skipped_reason?: string; notes?: string[];
}
export interface BulkResult {
  requested: number; enabled: number; updated: number; disabled: number;
  already_enabled: number; already_disabled: number; failed: number; changed: number;
  results: BulkRow[];
}

/** The settings the bulk dialog edits — a deliberate subset of TimelapseConfig.
 *  The backend overlays exactly these keys on each camera's own settings, so
 *  anything the dialog doesn't show (a PTZ preset, the skip rules) is kept per
 *  camera instead of being reset by a form that never displayed it. */
export type BulkSettings = Pick<TimelapseConfig, 'interval_s' | 'schedule' | 'resolution' | 'retention_days'>;

export function bulkSettingsFrom(cfg: TimelapseConfig): BulkSettings {
  return { interval_s: cfg.interval_s, schedule: cfg.schedule, resolution: cfg.resolution,
           retention_days: cfg.retention_days };
}

/** "3 switched on · 1 already on · 2 failed" — only the non-zero parts, in the
 *  order an operator cares about (what changed first, then what didn't). */
export function bulkSummary(r: BulkResult, turningOn: boolean): string {
  const parts: [number, string][] = turningOn
    ? [[r.enabled, 'switched on'], [r.updated, 'settings updated'], [r.already_enabled, 'already on']]
    : [[r.disabled, 'switched off'], [r.already_disabled, 'already off']];
  parts.push([r.failed, 'failed']);
  const out = parts.filter(([n]) => n > 0).map(([n, w]) => `${n} ${w}`);
  return out.length ? out.join(' · ') : 'Nothing to do';
}

/** Rows worth showing under the summary: every failure, and every success that
 *  carries a note (offline, camera disabled, PTZ without a preset). */
export function bulkAttention(r: BulkResult): BulkRow[] {
  return r.results.filter(x => !x.ok || (x.notes && x.notes.length > 0));
}
