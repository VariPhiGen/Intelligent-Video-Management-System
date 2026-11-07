/**
 * ptz.ts — the client half of PTZ control.
 *
 * One module, used by both surfaces (the live expanded panel and the config
 * tab), because the hard part is not the buttons — it is the *stop guarantee*,
 * and shipping two copies of that is shipping one broken copy.
 *
 * A continuous move latches: the camera keeps panning until something stops it.
 * The backend has its own guards (an ONVIF Timeout on the command and a
 * server-side watchdog — see services/ptz_control.py), and those are what make
 * a crashed tab safe. This module's job is to not need them in the normal case:
 *
 *   • a held direction re-issues every HEARTBEAT_MS, comfortably inside the
 *     server's stop timeout, so the camera never pauses mid-drag;
 *   • the move stops on pointerup, pointercancel, pointer-leave, blur, tab
 *     hide, component unmount, and Escape. Every one of those is a real way a
 *     drag ends without a mouseup — `pointercancel` alone covers a touch that
 *     turns into a scroll, which is most of the mobile cases.
 *
 * Pointer events, not mouse events, deliberately: pointer capture keeps a drag
 * bound to the pad even when the cursor slides off it, which is exactly what
 * happens when someone swings a camera hard left.
 */
import { useEffect, useState } from 'react';
import { apiFetch } from './api';
import type { Camera, PtzArchivedView, PtzCapabilities, PtzPreset } from './types';

/** Re-issue a held move at roughly half the server's stop timeout, so the
 *  camera is always re-armed well before the previous command expires. */
export const HEARTBEAT_MS = 700;
/** Drag-steering sends a changed direction at most this often (PtzDriver.steer). */
export const STEER_MS = 150;

export interface PtzInfo {
  camera_id: string;
  slug: string;
  capabilities: PtzCapabilities;
  held_by: string | null;
  move_timeout_seconds: number;
  default_speed: number;
}

export interface PtzStatus {
  pan: number | null;
  tilt: number | null;
  zoom: number | null;
  moving_pan_tilt: string | null;
  moving_zoom: string | null;
}

export type Axis = { pan?: number; tilt?: number; zoom?: number };

/** Where the head points in degrees (services/ptz_calibrate.degrees_of): + = right / up. */
export interface PtzAngles { elevation: number; right_of_home?: number; up_from_home?: number }

/** "32° left of Home · 8° below level" — the parts that are known. */
export function anglesText(a: PtzAngles | null | undefined): string {
  if (!a) return '';
  const parts: string[] = [];
  const side = (v: number, pos: string, neg: string) => (Math.abs(v) < 0.5 ? null : `${Math.round(Math.abs(v))}° ${v > 0 ? pos : neg}`);
  if (a.right_of_home != null || a.up_from_home != null) {
    const h = [side(a.right_of_home ?? 0, 'right', 'left'), side(a.up_from_home ?? 0, 'up', 'down')].filter(Boolean);
    parts.push(h.length ? `${h.join(', ')} from Home` : 'at Home');
  }
  parts.push(Math.abs(a.elevation) < 0.5 ? 'level' : `${Math.round(Math.abs(a.elevation))}° ${a.elevation > 0 ? 'above' : 'below'} level`);
  return parts.join(' · ');
}

/** From the calibration (services/ptz_calibrate.zoom_scale): the magnification now and the 1x/2x/4x… choices. */
export interface ZoomScale {
  mag: number | null;
  max_mag: number;
  /** above this the "x" is an estimate — calibration couldn't measure further */
  measured_to: number;
  steps: { mag: number; zoom: number; max?: boolean }[];
}

export type PtzPositionState = 'at_preset' | 'off_preset' | 'moving' | 'unknown';

/** Where a camera points now (services/ptz_track). */
export interface PtzPosition {
  state: PtzPositionState;
  preset_token: string | null;
  preset_id?: string | null;
  preset_name: string | null;
  pan: number | null;
  tilt: number | null;
  zoom: number | null;
  since: string;
  checked_at: string;
  cause: string | null;
  actor: string | null;
}

/** The preset the camera is CONFIRMED at — its picture checked, not just its reported position (services/ptz_track).
 *  What per-preset AI follows. state "none" while moving, off a preset, or still being checked ("arriving"). */
export interface PtzView {
  state: 'preset' | 'none';
  preset_id?: string | null;
  preset_token?: string | null;
  preset_name?: string | null;
  reason?: string;
  verified_by?: 'picture' | 'position';
  verified_at?: string;
}

/** A short line for the PTZ tab: is the view confirmed? null when there's nothing to say (moving / off preset). */
export function viewLabel(view: PtzView | null | undefined, pos: PtzPosition | null | undefined):
  { text: string; tone: 'ok' | 'wait' | 'warn'; title: string } | null {
  if (!view || pos?.state !== 'at_preset') return null;
  if (view.state === 'preset') {
    return view.verified_by === 'picture'
      ? { text: '✓ view confirmed', tone: 'ok', title: "The picture matches this preset's own picture." }
      : { text: '✓ view confirmed (position)', tone: 'ok',
          title: "The camera has stopped here; its picture couldn't be compared with the preset's (night, or no preset picture)." };
  }
  if (view.reason === 'arriving') return { text: 'checking the picture…', tone: 'wait', title: 'Confirming the camera really is at this preset.' };
  return { text: '⚠ view not confirmed', tone: 'warn', title: view.reason || '' };
}

export interface Page<T> { items: T[]; total: number; offset: number; limit: number }

export interface HistoryQuery {
  since?: string; until?: string; state?: string[]; cause?: string[]; preset?: string; actor?: string;
  offset?: number; limit?: number; camera_ids?: string[];
}

/** Query string for the history endpoints (also used for the CSV download link). */
export function historyParams(q: HistoryQuery, format: 'json' | 'csv' = 'json'): string {
  const p = new URLSearchParams();
  if (q.since) p.set('since', q.since);
  if (q.until) p.set('until', q.until);
  if (q.state?.length) p.set('state', q.state.join(','));
  if (q.cause?.length) p.set('cause', q.cause.join(','));
  if (q.preset) p.set('preset', q.preset);
  if (q.actor) p.set('actor', q.actor);
  if (q.camera_ids?.length) p.set('camera_ids', q.camera_ids.join(','));
  if (format === 'csv') p.set('format', 'csv');
  else { p.set('offset', String(q.offset ?? 0)); p.set('limit', String(q.limit ?? 50)); }
  return p.toString();
}

export interface TimeSplit { key: string; label: string; seconds: number; pct: number }

export interface PtzOverviewRow {
  id: string; name: string; slug: string; enabled: boolean; health_status: string;
  position: PtzPosition | null; presets: number; presets_missing: number;
  calibrated: boolean; calibrated_at: string | null; max_presets: number | null;
  calibration_angles?: boolean; calibration_levels?: number;
  /** live state of a queued / running / finished calibration (Cameras → PTZ bulk calibrate) */
  calibration_run?: { state: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'; step?: string | null;
                      error?: string | null; note?: string | null; started_at?: string; finished_at?: string } | null;
  queue_position?: number | null;
}

export interface PtzPositionRow {
  camera_id?: string;
  camera_name?: string | null;
  at: string;
  state: PtzPositionState;
  preset_token: string | null;
  preset_name: string | null;
  pan: number | null;
  tilt: number | null;
  zoom: number | null;
  cause: string | null;
  actor: string | null;
}

export interface PtzCalibration {
  notes?: string[];
  space: 'absolute' | 'relative';
  points: { zoom: number; pan_per_width: number; tilt_per_height: number }[];
  measured_at: string;
  by?: string;
  /** measured angles (degrees per unit, the horizon's tilt); null = straight-line model */
  geometry?: { kt: number; t0: number; kp: number } | null;
  /** what the after-click checks learned, per zoom band */
  learned?: Record<string, { pan?: number; tilt?: number; n: number }>;
}

export interface PtzCentreCheck {
  checked: boolean;
  corrected: [number, number] | null;
  residual?: [number, number];
  why?: string;
  at: string;
}

export interface PtzCalibrationRun {
  state: 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
  step?: string | null;
  error?: string | null;
  notes?: string[];
  started_at?: string;
}

const CAUSE_WORD: Record<string, string> = {
  operator: 'operator', preset: 'preset recall', home: 'go home', patrol: 'patrol',
  calibration: 'calibration', camera: 'the camera itself',
};

/** The camera's home position in position data (services/ptz_track.HOME_TOKEN) — not a camera preset. */
export const HOME_TOKEN = '__home__';

/** "📍 Gate", "⌂ Home", "Moving…", "Off preset", "Position unknown" — the short label for a tile. */
export function positionLabel(p: PtzPosition | null | undefined): string {
  if (!p) return '';
  if (p.state === 'at_preset') return p.preset_token === HOME_TOKEN ? '⌂ Home' : `📍 ${p.preset_name || 'preset'}`;
  if (p.state === 'moving') return 'Moving…';
  if (p.state === 'off_preset') return 'Off preset';
  return 'Position unknown';
}

/** "by ramesh (operator)" / "patrol" / "" — who or what put it there. */
export function causeText(cause: string | null | undefined, actor: string | null | undefined): string {
  if (!cause) return '';
  const word = CAUSE_WORD[cause] || cause;
  return actor ? `${actor} (${word})` : word;
}

/** Live-grid hook: current positions for the PTZ cameras on screen, refreshed every `everyMs`. */
export function usePtzPositions(ids: string[], everyMs = 5000): Record<string, PtzPosition> {
  const [map, setMap] = useState<Record<string, PtzPosition>>({});
  const key = ids.join(',');
  useEffect(() => {
    if (!ids.length) { setMap({}); return; }
    let alive = true;
    const load = () => ptzApi.positions(ids).then(r => { if (alive) setMap(r.positions || {}); }).catch(() => {});
    load();
    const t = setInterval(load, everyMs);
    return () => { alive = false; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, everyMs]);
  return map;
}

/**
 * The rectangle the PICTURE occupies inside a box that shows it with `object-fit: contain`. The live view
 * letterboxes (black bars when the panel's shape differs from the camera's), and click-to-centre measured clicks
 * against the whole panel — bars included — so on a wide panel every click was off by the bar width.
 */
export function containRect(box: { left: number; top: number; width: number; height: number },
                            videoW: number, videoH: number): { left: number; top: number; width: number; height: number } {
  if (!videoW || !videoH || !box.width || !box.height) return box;
  const scale = Math.min(box.width / videoW, box.height / videoH);
  const w = videoW * scale, h = videoH * scale;
  return { left: box.left + (box.width - w) / 2, top: box.top + (box.height - h) / 2, width: w, height: h };
}

/** The picture rect of the <video> that shares a parent with `layer` (falls back to the layer's own box). */
export function pictureRect(layer: HTMLElement): { left: number; top: number; width: number; height: number } {
  const video = layer.parentElement?.querySelector('video') as HTMLVideoElement | null;
  const box = (video || layer).getBoundingClientRect();
  return video ? containRect(box, video.videoWidth, video.videoHeight) : box;
}

/**
 * The picture the operator is looking at, as a small JPEG (base64, no data: prefix) — sent with a click so the
 * server can check where the clicked spot actually landed (services/ptz_calibrate.verify_centre). null when it
 * can't be had: no <video> yet, not playing, or a cross-origin stream the browser won't let us read. No frame
 * just means no check; the click still works.
 */
export function grabFrame(layer: HTMLElement, width = 480): string | null {
  const video = layer.parentElement?.querySelector('video') as HTMLVideoElement | null;
  if (!video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) return null;
  try {
    const c = document.createElement('canvas');
    c.width = width;
    c.height = Math.round(width * video.videoHeight / video.videoWidth);
    c.getContext('2d')?.drawImage(video, 0, 0, c.width, c.height);
    const url = c.toDataURL('image/jpeg', 0.85);
    return url.startsWith('data:image/jpeg') ? url.slice(url.indexOf(',') + 1) : null;
  } catch {
    return null;   // a tainted canvas (cross-origin stream) throws on read
  }
}

/** "3.2×", "12×", "1×" — the magnification, one decimal below 10. */
export function magLabel(m: number | null | undefined): string {
  if (!m || !Number.isFinite(m)) return '';
  return `${m < 9.95 ? (Math.round(m * 10) / 10).toString() : Math.round(m).toString()}×`;
}

/**
 * The pad's speed at this magnification: divided by it, so at 12× a held arrow sweeps about as many frame widths
 * per second as at 1× — undivided it throws the scene off-screen before anyone can let go. Floored so the slowest
 * setting still moves on cameras with a coarse speed space.
 */
export function zoomedSpeed(speed: number, mag: number | null | undefined): number {
  const m = mag && mag > 1 ? mag : 1;
  return Math.max(0.03, speed / m);
}

/**
 * Drag-steering on the pad: the pointer's offset from the pad's centre → a direction and a speed fraction.
 * At the arrows (2/3 of the radius) and beyond = full speed; toward the hub slower; inside the hub = nothing.
 */
export function steerVector(dx: number, dy: number, radius: number): { pan: number; tilt: number } | null {
  const dist = Math.hypot(dx, dy);
  if (!radius || dist < radius * 0.18) return null;
  const f = Math.min(1, Math.max(0.15, dist / (radius * 0.67)));
  return { pan: (dx / dist) * f, tilt: (-dy / dist) * f };
}

// ─── Zoom as people read it ──────────────────────────────────────────────────
//
// The camera's zoom number means nothing to an operator (i-PRO: 0..0.09375). Everywhere zoom is shown or chosen
// it reads the same way: a calibrated camera in MAGNIFICATION ("3.2×", measured), any other in PERCENT of its zoom
// range ("35 %"). The pad's chips go to 1× / 2× / 4× … / Max, or Wide / 25 / 50 / 75 % / Tele.

/** The camera's zoom range: its reported limits, else ONVIF's generic 0..1 when it zooms to a position at all. */
export function zoomRange(caps: PtzCapabilities | null | undefined): [number, number] | null {
  const r = caps?.ranges?.zoom;
  if (Array.isArray(r) && r.length === 2 && r[1] > r[0]) return [r[0], r[1]];
  return caps?.absolute?.zoom ? [0, 1] : null;
}

const EXTRAPOLATE_MAX = 2.5;   // = ptz_calibrate._EXTRAPOLATE_MAX

/**
 * Magnification at `zoom` from a calibration — the client copy of services/ptz_calibrate.magnification
 * (log-space interpolation of pan-per-width, at most 2.5× more past the last measured level). For tables, where
 * a round trip per row would be silly; the pad itself uses the server's zoom_scale.
 */
export function magAt(cal: Pick<PtzCalibration, 'points'> | null | undefined, zoom: number | null | undefined): number | null {
  if (zoom == null) return null;
  const pts = (cal?.points || []).filter(p => p.pan_per_width).map(p => [p.zoom, Math.abs(p.pan_per_width)] as const)
    .sort((a, b) => a[0] - b[0]);
  if (pts.length < 2) return null;
  const along = (a: readonly [number, number], b: readonly [number, number], z: number) => {
    const t = b[0] === a[0] ? 0 : (z - a[0]) / (b[0] - a[0]);
    return Math.exp(Math.log(a[1]) + (Math.log(b[1]) - Math.log(a[1])) * t);
  };
  let v: number;
  const last = pts[pts.length - 1];
  if (zoom <= pts[0][0]) v = along(pts[0], pts[1], zoom);
  else if (zoom >= last[0]) v = Math.min(last[1], Math.max(along(pts[pts.length - 2], last, zoom), last[1] / EXTRAPOLATE_MAX));
  else {
    const i = pts.findIndex((p, k) => k > 0 && zoom <= p[0]);
    v = along(pts[i - 1], pts[i], zoom);
  }
  return pts[0][1] / v;
}

/** Percent of the zoom range, 0..100. */
export function zoomPercent(zoom: number | null | undefined, range: [number, number] | null): number | null {
  if (zoom == null || !range) return null;
  return Math.round(Math.max(0, Math.min(1, (zoom - range[0]) / (range[1] - range[0]))) * 100);
}

/** "3.2×" (calibrated) · "35%" (range known) · the raw number (neither) · "—" (no zoom). */
export function zoomText(zoom: number | null | undefined, cal: Pick<PtzCalibration, 'points'> | null | undefined,
                         range: [number, number] | null): string {
  if (zoom == null) return '—';
  const m = magAt(cal, zoom);
  if (m) return magLabel(m);
  const pc = zoomPercent(zoom, range);
  return pc != null ? `${pc}%` : zoom.toFixed(3);
}

/** The chips for an uncalibrated camera that can go to a zoom position. */
export function percentSteps(range: [number, number]): { label: string; zoom: number; title: string }[] {
  const at = (f: number) => Number((range[0] + (range[1] - range[0]) * f).toFixed(5));
  return [
    { label: 'Wide', zoom: at(0), title: 'All the way out' },
    { label: '25%', zoom: at(0.25), title: 'A quarter of the zoom range' },
    { label: '50%', zoom: at(0.5), title: 'Half the zoom range' },
    { label: '75%', zoom: at(0.75), title: 'Three quarters of the zoom range' },
    { label: 'Tele', zoom: at(1), title: 'All the way in' },
  ];
}

// One calibration fetch per camera per page load, shared by every table on it; CalibrationPanel forgets it after
// a (re)calibration or a clear.
const calCache = new Map<string, Promise<PtzCalibration | null>>();

export function forgetCalibration(cameraId: string): void {
  calCache.delete(cameraId);
}

/** A formatter for zoom values of one camera (see zoomText) — the raw number until the calibration has loaded. */
export function useZoomText(cameraId: string | undefined, caps: PtzCapabilities | null | undefined): (z: number | null | undefined) => string {
  const [cal, setCal] = useState<PtzCalibration | null>(null);
  useEffect(() => {
    setCal(null);
    if (!cameraId) return;
    let alive = true;
    let p = calCache.get(cameraId);
    if (!p) {
      p = ptzApi.calibration(cameraId).then(r => r.calibration).catch(() => { calCache.delete(cameraId); return null; });
      calCache.set(cameraId, p);
    }
    p.then(c => { if (alive) setCal(c); });
    return () => { alive = false; };
  }, [cameraId]);
  const range = zoomRange(caps);
  return (z) => zoomText(z, cal, range);
}

/** The click/box maths the server needs: a point in -1..1 from the frame centre (+y up). */
export function framePoint(clientX: number, clientY: number,
                           rect: { left: number; top: number; width: number; height: number }): { x: number; y: number } {
  const x = (clientX - rect.left) / rect.width * 2 - 1;
  const y = 1 - (clientY - rect.top) / rect.height * 2;
  const c = (v: number) => Math.max(-1, Math.min(1, Number(v.toFixed(4))));
  return { x: c(x), y: c(y) };
}

/** A drawn box → its centre and how much to zoom so it fills the frame (null for a sliver/near-click). */
export function boxToCentre(x0: number, y0: number, x1: number, y1: number,
                            rect: { left: number; top: number; width: number; height: number },
                            minBoxFrac = 0.03): { x: number; y: number; zoom_factor: number } | null {
  const a = framePoint(x0, y0, rect), b = framePoint(x1, y1, rect);
  const w = Math.abs(b.x - a.x) / 2, h = Math.abs(b.y - a.y) / 2;
  if (w < minBoxFrac || h < minBoxFrac) return null;
  const fit = Math.max(w, h);                       // the box's larger side fills the frame → the whole box stays in view
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, zoom_factor: Math.min(50, Number((1 / fit).toFixed(3))) };
}

export const ptzApi = {
  info: (id: string, refresh = false) =>
    apiFetch<PtzInfo>(`/cameras/${id}/ptz${refresh ? '?refresh=true' : ''}`),
  detect: (id: string) =>
    apiFetch<{ capabilities: PtzCapabilities }>(`/cameras/${id}/ptz/detect`, { method: 'POST' }),
  move: (id: string, v: Axis) =>
    apiFetch(`/cameras/${id}/ptz/move`, { method: 'POST', body: JSON.stringify(v) }),
  stop: (id: string) =>
    apiFetch(`/cameras/${id}/ptz/stop`, { method: 'POST' }),
  step: (id: string, v: Axis & { speed?: number }) =>
    apiFetch(`/cameras/${id}/ptz/step`, { method: 'POST', body: JSON.stringify(v) }),
  status: (id: string) =>
    apiFetch<{ status: PtzStatus | null; held_by: string | null; zoom_scale?: ZoomScale | null;
               angles?: PtzAngles | null; home_known?: boolean }>(`/cameras/${id}/ptz/status`),
  /** Turn by degrees (needs measured angles): `right`/`up` (negative = left/down) from "current" or "home". */
  aim: (id: string, body: { right: number; up: number; origin: 'current' | 'home'; up_from_level?: boolean; speed?: number }) =>
    apiFetch<{ pan: number; tilt: number; clamped: boolean }>(`/cameras/${id}/ptz/aim`, { method: 'POST', body: JSON.stringify(body) }),
  /** Send the camera to its own home and remember where that is (moves the camera). */
  learnHome: (id: string) =>
    apiFetch(`/cameras/${id}/ptz/home/learn`, { method: 'POST' }),
  absolute: (id: string, v: Axis & { speed?: number }) =>
    apiFetch(`/cameras/${id}/ptz/absolute`, { method: 'POST', body: JSON.stringify(v) }),
  presets: (id: string) =>
    apiFetch<{ presets: PtzPreset[] }>(`/cameras/${id}/ptz/presets`),
  takeReference: (id: string, token: string) =>
    apiFetch<{ preset: PtzPreset; night: boolean }>(`/cameras/${id}/ptz/presets/${encodeURIComponent(token)}/reference`,
             { method: 'POST' }),
  /** The view moved under the zones: "accept" = they're right on the new picture; "undo" = put them back. */
  reviewZones: (id: string, token: string, action: 'accept' | 'undo') =>
    apiFetch<{ preset: PtzPreset }>(`/cameras/${id}/ptz/presets/${encodeURIComponent(token)}/review`,
             { method: 'POST', body: JSON.stringify({ action }) }),
  /** Zones + rules of deleted presets, newest first. */
  archivedViews: (id: string) => apiFetch<{ archived: PtzArchivedView[] }>(`/cameras/${id}/ptz/archived-views`),
  copyArchivedView: (id: string, archiveId: string, presetToken: string, replace = false) =>
    apiFetch<{ preset: PtzPreset }>(`/cameras/${id}/ptz/archived-views/${archiveId}/copy`,
             { method: 'POST', body: JSON.stringify({ preset_token: presetToken, replace }) }),
  forgetArchivedView: (id: string, archiveId: string) =>
    apiFetch(`/cameras/${id}/ptz/archived-views/${archiveId}`, { method: 'DELETE' }),
  gotoPreset: (id: string, token: string, speed?: number) =>
    apiFetch(`/cameras/${id}/ptz/presets/${encodeURIComponent(token)}/goto`,
             { method: 'POST', body: JSON.stringify({ speed }) }),
  savePreset: (id: string, name: string, token?: string) =>
    apiFetch<{ preset_token: string }>(`/cameras/${id}/ptz/presets`,
             { method: 'PUT', body: JSON.stringify({ name, token }) }),
  deletePreset: (id: string, token: string) =>
    apiFetch(`/cameras/${id}/ptz/presets/${encodeURIComponent(token)}`, { method: 'DELETE' }),
  patchPreset: (id: string, token: string, body: { name?: string; is_home?: boolean; sort_order?: number }) =>
    apiFetch<{ preset: PtzPreset }>(`/cameras/${id}/ptz/presets/${encodeURIComponent(token)}`,
             { method: 'PATCH', body: JSON.stringify(body) }),
  refreshThumbnail: (id: string, token: string) =>
    apiFetch<{ preset: PtzPreset }>(`/cameras/${id}/ptz/presets/${encodeURIComponent(token)}/thumbnail`, { method: 'POST' }),
  restorePreset: (id: string, token: string) =>
    apiFetch<{ preset: PtzPreset }>(`/cameras/${id}/ptz/presets/${encodeURIComponent(token)}/restore`, { method: 'POST' }),
  /** Click-to-centre / drag-to-zoom: (x, y) in -1..1 from the frame centre, +y UP. The server does the maths from
   *  the camera's measured field of view (services/ptz_calibrate). */
  centre: (id: string, body: { x: number; y: number; zoom_factor?: number; speed?: number; frame?: string }) =>
    apiFetch<{ mode: string; calibrated: boolean; checking?: boolean }>(`/cameras/${id}/ptz/centre`, { method: 'POST', body: JSON.stringify(body) }),
  position: (id: string) =>
    apiFetch<{ position: PtzPosition | null; view?: PtzView | null }>(`/cameras/${id}/ptz/position`),
  positions: (ids: string[]) =>
    apiFetch<{ positions: Record<string, PtzPosition> }>(`/cameras/ptz/positions?ids=${encodeURIComponent(ids.join(','))}`),
  history: (id: string, q: HistoryQuery = {}) =>
    apiFetch<Page<PtzPositionRow>>(`/cameras/${id}/ptz/positions?${historyParams(q)}`),
  historyAll: (q: HistoryQuery & { camera_ids?: string[] } = {}) =>
    apiFetch<Page<PtzPositionRow>>(`/cameras/ptz/history?${historyParams(q)}`),
  positionAt: (id: string, at: string) =>
    apiFetch<{ at: string; position: PtzPositionRow | null }>(`/cameras/${id}/ptz/position-at?at=${encodeURIComponent(at)}`),
  summary: (id: string, since: string, until?: string) =>
    apiFetch<{ split: TimeSplit[] }>(`/cameras/${id}/ptz/positions/summary?since=${encodeURIComponent(since)}${until ? `&until=${encodeURIComponent(until)}` : ''}`),
  overview: () => apiFetch<{ cameras: PtzOverviewRow[] }>(`/cameras/ptz/overview`),
  orderPresets: (id: string, tokens: string[]) =>
    apiFetch<{ presets: PtzPreset[] }>(`/cameras/${id}/ptz/presets/order`, { method: 'PUT', body: JSON.stringify({ tokens }) }),
  calibration: (id: string) =>
    apiFetch<{ calibration: PtzCalibration | null; run: PtzCalibrationRun | null; last_check?: PtzCentreCheck | null }>(`/cameras/${id}/ptz/calibration`),
  /** How long movement history is kept, and how much room it takes (needs "PTZ movement history"). */
  settings: () => apiFetch<{ history: { operator_days: number; patrol_days: number };
                             limits: Record<string, [number, number]>; bytes: Record<string, number | null> }>(`/cameras/ptz/settings`),
  /** Admin: set history retention. */
  saveSettings: (history: { operator_days: number; patrol_days: number }) =>
    apiFetch<{ history: { operator_days: number; patrol_days: number } }>(`/cameras/ptz/settings`,
             { method: 'PUT', body: JSON.stringify({ history }) }),
  /** Calibrate these cameras one at a time (server-side queue). */
  queueCalibration: (ids: string[]) =>
    apiFetch<{ added: string[]; skipped: { camera_id: string; why: string }[] }>(`/cameras/ptz/calibration-queue`,
             { method: 'POST', body: JSON.stringify({ camera_ids: ids }) }),
  /** Take one camera (or all) off the queue; `stopRunning` also stops the one calibrating now. */
  unqueueCalibration: (cameraId?: string, stopRunning = false) =>
    apiFetch(`/cameras/ptz/calibration-queue?${new URLSearchParams({
      ...(cameraId ? { camera_id: cameraId } : {}), include_running: String(stopRunning) })}`, { method: 'DELETE' }),
  calibrate: (id: string) =>
    apiFetch(`/cameras/${id}/ptz/calibrate`, { method: 'POST' }),
  clearCalibration: (id: string) =>
    apiFetch(`/cameras/${id}/ptz/calibration`, { method: 'DELETE' }),
  gotoHome: (id: string, speed?: number) =>
    apiFetch(`/cameras/${id}/ptz/home`, { method: 'POST', body: JSON.stringify({ speed }) }),
  setHome: (id: string) =>
    apiFetch(`/cameras/${id}/ptz/home`, { method: 'PUT' }),
};

/**
 * A live continuous-move session for one camera.
 *
 * Not a React hook so the live grid can hold one per expanded camera without
 * re-render coupling; `usePtzDriver` below wraps it for components.
 */
export class PtzDriver {
  private timer: ReturnType<typeof setInterval> | null = null;
  private axis: Axis | null = null;
  private stopping = false;

  constructor(
    private cameraId: string,
    private onError: (msg: string) => void,
    private onMovingChange?: (moving: boolean) => void,
  ) {}

  /** Begin (or redirect) a held move. Safe to call repeatedly with the same
   *  axis — a direction change swaps the velocity without a stop/start gap,
   *  which is what makes a diagonal drag feel continuous. */
  start(axis: Axis): void {
    this.axis = axis;
    this.stopping = false;
    void this.tick();
    if (this.timer === null) {
      this.timer = setInterval(() => { void this.tick(); }, HEARTBEAT_MS);
      this.onMovingChange?.(true);
    }
  }

  private async tick(): Promise<void> {
    if (!this.axis || this.stopping) return;
    try {
      await ptzApi.move(this.cameraId, this.axis);
    } catch (e: any) {
      // A refused move must not keep re-firing: a 409 (someone else holds the
      // camera, or it does not pan) would otherwise repeat forever behind a
      // held button and bury the operator in identical toasts.
      this.onError(e?.message || 'PTZ move failed');
      void this.stop();
    }
  }

  /** End the move. Idempotent, and safe to call from an unmount path — the
   *  server-side watchdog is the backstop if this request never lands. */
  async stop(): Promise<void> {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (!this.axis) return;
    this.axis = null;
    this.stopping = true;
    this.onMovingChange?.(false);
    try {
      await ptzApi.stop(this.cameraId);
    } catch {
      /* The camera stops itself on the ONVIF timeout, and the server watchdog
         covers the firmwares that ignore it. Nothing useful to say here. */
    }
  }

  /** Redirect a held move (drag-steering). Pointer moves fire ~60×/s; this sends at most every STEER_MS and lets
   *  the heartbeat carry the latest direction in between, so a drag is not 60 requests a second. */
  steer(axis: Axis): void {
    if (this.timer === null) { this.start(axis); this.lastSent = Date.now(); return; }
    this.axis = axis;
    if (Date.now() - this.lastSent >= STEER_MS) {
      this.lastSent = Date.now();
      void this.tick();
    }
  }

  private lastSent = 0;

  get moving(): boolean {
    return this.axis !== null;
  }
}

/**
 * Resolve a camera's PTZ capabilities, PROBING THE DEVICE once when it has
 * never been checked.
 *
 * This closes a chicken-and-egg the first cut shipped: the backend probes on
 * credential entry and on first use of a /ptz endpoint, but discovery stores
 * credentials at registration (credential entry never happens) and the UI
 * gated every /ptz call on `ptz_capable` already being true (first use never
 * happens). A discovery-onboarded PTZ dome therefore sat at "not checked yet"
 * forever, with nothing on screen able to check it.
 *
 * GET /cameras/{id}/ptz IS the check — the backend probes on a NULL cache and
 * persists the answer, so this costs one SOAP round trip per camera ever.
 * Deduped module-wide because the live panel and the config page can both ask
 * during the seconds a probe takes. A FAILED check (camera unreachable) is
 * dropped from the map so a later visit retries — the backend deliberately
 * refuses to cache "unreachable" as "fixed", and so do we.
 */
const checkInFlight = new Map<string, Promise<PtzCapabilities | null>>();

export function ensurePtzChecked(
  cam: Pick<Camera, 'id' | 'onvif_capable' | 'ptz'>,
): Promise<PtzCapabilities | null> {
  if (cam.ptz) return Promise.resolve(cam.ptz);
  if (!cam.onvif_capable) return Promise.resolve(null);
  let p = checkInFlight.get(cam.id);
  if (!p) {
    p = ptzApi.info(cam.id)
      .then(r => r.capabilities)
      .catch(() => { checkInFlight.delete(cam.id); return null; });
    checkInFlight.set(cam.id, p);
  }
  return p;
}

/**
 * The component-side view of the check: `caps` once known (from the camera
 * row, or freshly probed), `checking` while the device is being asked,
 * `failed` when the camera could not be reached to ask. `onKnown` fires once
 * when a fresh probe lands, so a page can refresh the camera list and let
 * `ptz_capable` (and the config PTZ tab) propagate everywhere else.
 */
export function usePtzCapabilities(cam: Camera, onKnown?: () => void) {
  const needsCheck = cam.onvif_capable && cam.ptz === null;
  const [fresh, setFresh] = useState<PtzCapabilities | null>(null);
  const [checking, setChecking] = useState(needsCheck);

  useEffect(() => {
    setFresh(null);
    if (!needsCheck) { setChecking(false); return; }
    let alive = true;
    setChecking(true);
    ensurePtzChecked(cam).then(caps => {
      if (!alive) return;
      setFresh(caps);
      setChecking(false);
      if (caps) onKnown?.();
    });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cam.id, needsCheck]);

  const caps = cam.ptz ?? fresh;
  return { caps, checking, failed: needsCheck && !checking && caps === null };
}

/** Which capability block a control needs before it may be offered at all. */
export const can = (c: PtzCapabilities | null | undefined,
                    kind: 'continuous' | 'relative' | 'absolute',
                    axis: 'pan_tilt' | 'zoom'): boolean =>
  Boolean(c?.supported && (c as any)[kind]?.[axis]);

// SETTLE_POLL_MS / SETTLE_MAX_WAIT_MS: waitForSettle below.
export const SETTLE_POLL_MS = 180;
export const SETTLE_MAX_WAIT_MS = 2000;

/**
 * True once every axis PtzClickLayer actually moved has stopped reporting
 * "MOVING" in a GetStatus poll. Pure and separate from waitForSettle so the
 * decision logic is unit-testable without real timers or a network call —
 * `moved` only names the axes the CALLER moved (the click gesture never
 * drives zoom, so a camera whose zoom motor happens to be moving from
 * something else must not block a pan/tilt click on it finishing).
 */
export function axesSettled(
  status: Pick<PtzStatus, 'moving_pan_tilt' | 'moving_zoom'> | null | undefined,
  moved: { panTilt?: boolean; zoom?: boolean },
): boolean {
  if (!status) return false;   // no status this poll -- caller keeps polling or times out
  const panTiltDone = !moved.panTilt || status.moving_pan_tilt !== 'MOVING';
  const zoomDone = !moved.zoom || status.moving_zoom !== 'MOVING';
  return panTiltDone && zoomDone;
}

/**
 * waitForSettle — poll GetStatus (via ptzApi.status, i.e. /ptz/status) until
 * the camera itself reports the axes PtzClickLayer just moved are no longer
 * MOVING, or SETTLE_MAX_WAIT_MS elapses.
 *
 * Replaces an earlier fixed-timeout guess. Confirmed live (2026-09-19,
 * KEC01's PT1) that MoveStatus is real and fast on real hardware — MOVING
 * immediately after a RelativeMove, IDLE by the next ~300ms poll — so polling
 * beats guessing: most clicks unlock in one or two polls instead of the
 * click-to-centre gesture waiting out a worst-case timer on every single
 * click. SETTLE_MAX_WAIT_MS is the fallback for a camera whose MoveStatus
 * never reports IDLE (a real failure mode on cheaper ONVIF stacks — vendors
 * under-report GetStatus the same way they under-report GetNodes, see
 * onvif_ptz.py's module docstring) — without a ceiling, such a camera would
 * lock the click gesture forever after its very first move.
 */
export async function waitForSettle(
  cameraId: string,
  moved: { panTilt?: boolean; zoom?: boolean },
): Promise<void> {
  const deadline = Date.now() + SETTLE_MAX_WAIT_MS;
  while (Date.now() < deadline) {
    try {
      const { status } = await ptzApi.status(cameraId);
      if (axesSettled(status, moved)) return;
    } catch {
      // A failed poll must not spin faster than a successful one -- fall
      // through to the same wait and try again, up to the deadline.
    }
    await new Promise(resolve => setTimeout(resolve, SETTLE_POLL_MS));
  }
}

/**
 * clickToRelativeMove — convert a click's pixel offset within a rendered video
 * frame into the RELATIVE pan/tilt translation PtzClickLayer's click-to-centre
 * gesture sends. Pure (takes a plain rect, not a DOM event) so it's unit-
 * testable without a real click or a mounted component — see PtzClickLayer in
 * pages/live/PtzControl.tsx for the ONVIF-call side and why `gain` is a
 * deliberate under-shoot rather than 1:1.
 *
 * `deadZone` treats a click within a few percent of centre as a mis-click
 * rather than a request to nudge the camera imperceptibly.
 */
export function clickToRelativeMove(
  clientX: number, clientY: number,
  rect: { left: number; top: number; width: number; height: number },
  gain: number, deadZone = 0.04,
): { pan: number; tilt: number } | null {
  const fx = (clientX - rect.left) / rect.width * 2 - 1;     // -1 left .. +1 right
  const fy = 1 - (clientY - rect.top) / rect.height * 2;     // ONVIF tilt is +1 UP
  if (Math.abs(fx) < deadZone && Math.abs(fy) < deadZone) return null;
  return { pan: Number((fx * gain).toFixed(4)), tilt: Number((fy * gain).toFixed(4)) };
}

/**
 * boxToZoomMove — convert a drawn rectangle (drag-to-zoom-in) into a single
 * RELATIVE pan/tilt/zoom move: centre on the box's midpoint (same maths as
 * clickToRelativeMove, just fed the midpoint of two points instead of one)
 * and zoom in proportionally to how much smaller the box is than the frame.
 *
 * There is no ONVIF "fit this rectangle" operation, and unlike centring,
 * there isn't even an honest way to compute the REQUIRED zoom step: the
 * normalised -1..1 zoom range's relationship to actual optical field-of-view
 * is vendor/lens-specific and not reported over ONVIF, the same reason
 * `centreGain` on the pan/tilt side is a deliberate under-shoot rather than a
 * precise calculation. So this is frankly a heuristic, gain-limited the same
 * way: `zoomGain` under-shoots so a drawn box converges over a couple of
 * drags rather than either doing nothing or slamming to max zoom on a small
 * box drawn near the lens's limit.
 *
 * Takes the SMALLER of the box's width/height fraction (not, say, the
 * average) so the zoom step is sized to fit the whole box in view — the
 * user's ask was "zoom till that box is in the view", and using the larger
 * dimension would let the other edge spill past frame bounds.
 *
 * Two independent rejection guards, not one: `minPxDrag` (checked by the
 * CALLER against raw screen pixels, not here) decides click vs. drag at all;
 * `minBoxFrac` here catches a drag that moved plenty of screen pixels but
 * produced a degenerate sliver box (near-straight-line drag) — a real
 * possibility on a large/high-resolution video panel where a drag can clear
 * the pixel threshold while covering under a percent of frame width.
 */
export function boxToZoomMove(
  x0: number, y0: number, x1: number, y1: number,
  rect: { left: number; top: number; width: number; height: number },
  centreGain: number, zoomGain: number, zoomMax: number,
  minBoxFrac = 0.03,
): { pan: number; tilt: number; zoom: number } | null {
  const fx0 = (x0 - rect.left) / rect.width * 2 - 1;
  const fy0 = 1 - (y0 - rect.top) / rect.height * 2;
  const fx1 = (x1 - rect.left) / rect.width * 2 - 1;
  const fy1 = 1 - (y1 - rect.top) / rect.height * 2;

  const widthFrac = Math.abs(fx1 - fx0) / 2;    // 0..1 of frame width
  const heightFrac = Math.abs(fy1 - fy0) / 2;   // 0..1 of frame height
  if (widthFrac < minBoxFrac || heightFrac < minBoxFrac) return null;

  const midX = (fx0 + fx1) / 2;
  const midY = (fy0 + fy1) / 2;
  const fitFrac = Math.min(widthFrac, heightFrac, 1);
  const rawZoom = 1 - fitFrac;   // 0 when the box is the whole frame already; -> 1 as it shrinks toward a point

  return {
    pan: Number((midX * centreGain).toFixed(4)),
    tilt: Number((midY * centreGain).toFixed(4)),
    zoom: Number(Math.min(zoomMax, Math.max(0, rawZoom * zoomGain)).toFixed(4)),
  };
}

/**
 * accumulatedZoomStep — collapse a burst of wheel deltas into one ONVIF
 * relative-zoom fraction. PtzClickLayer batches every `deltaY` from a scroll
 * gesture (a trackpad fires a dozen small events for one flick) and calls this
 * once the gesture pauses, rather than sending a RelativeMove per tick — see
 * its ZOOM_DEBOUNCE_MS comment for why a command per tick would queue a burst
 * the camera has to work through one at a time, the same stacking problem the
 * click busy-lock exists for.
 *
 * Scroll up (negative deltaY) zooms in — positive/tele, per the ONVIF axis
 * convention documented in onvif_ptz.py ("zoom x, +1 tele"). `max` clamps so a
 * fast fling can't fling the zoom motor just as hard; a result smaller than
 * `minStep` floors to 0 so trackpad jitter after the real gesture ends doesn't
 * send an imperceptible extra step.
 */
export function accumulatedZoomStep(
  deltaSum: number, scale: number, max: number, minStep = 0.01,
): number {
  const zoom = Math.max(-max, Math.min(max, -deltaSum * scale));
  return Math.abs(zoom) < minStep ? 0 : zoom;
}


/** Time-range presets for the history filters. */
export const RANGES: { key: string; label: string; hours: number | null }[] = [
  { key: '1h', label: 'Last hour', hours: 1 }, { key: '24h', label: 'Last 24 h', hours: 24 },
  { key: '7d', label: 'Last 7 days', hours: 168 }, { key: '30d', label: 'Last 30 days', hours: 720 },
  { key: '90d', label: 'All (90 days)', hours: 2160 }, { key: 'custom', label: 'Custom…', hours: null },
];

export const STATE_OPTIONS = [
  { value: 'at_preset', label: 'At a preset' }, { value: 'off_preset', label: 'Off preset' },
  { value: 'moving', label: 'Moving' }, { value: 'unknown', label: 'Unknown' },
];

export const CAUSE_OPTIONS = [
  { value: 'operator', label: 'Operator' }, { value: 'preset', label: 'Preset recall' },
  { value: 'home', label: 'Go home' }, { value: 'patrol', label: 'Patrol' },
  { value: 'camera', label: 'The camera itself' }, { value: 'calibration', label: 'Calibration' },
  { value: 'unknown', label: 'Not known (first reading)' },
];

export function fmtDuration(s: number): string {
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} d`;
}
