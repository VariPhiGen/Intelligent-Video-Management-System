/**
 * Camera list filters — one definition shared by the Live grid rail and the
 * Cameras inventory, so "PTZ" or "Offline" means the same camera set on both.
 *
 * Every facet is read off fields the camera list already carries; nothing here
 * asks the device. Facets are single-choice toggles (click the active chip to
 * clear it) and combine with AND.
 *
 *   status     online = health_status "connected"; disabled cameras are their
 *              own value, not "offline" — nobody expects them to stream.
 *   type       ptz = the DEVICE said it pans (ptz_capable); fixed = it was asked
 *              and said no; unchecked = never asked (ptz null). The last two are
 *              kept apart on purpose: a camera whose ONVIF login never worked
 *              was never asked, and calling it "fixed" hides why its PTZ
 *              controls are missing.
 *   maker      ONVIF Manufacturer, grouped ignoring case, spaces and punctuation
 *              ("CPPLUS", "CP PLUS", "Cp-Plus" are one maker); "Unknown" when
 *              ONVIF never answered.
 *   recording  the camera's own recording switch.
 *   attention  "No ONVIF login": the camera has an address but no working
 *              stored ONVIF credentials, so PTZ, stream settings, imaging and
 *              maintenance can't work (onvif_capable false).
 */
import type { Camera } from './types';

export type CamStatus = 'online' | 'offline' | 'disabled';
export type CamType = 'ptz' | 'fixed' | 'unchecked';

export interface CameraFilter {
  status?: CamStatus;
  type?: CamType;
  /** Upper-cased maker key (see makerKey). */
  maker?: string;
  recording?: 'on' | 'off';
  attention?: boolean;
}

export const STATUS_LABEL: Record<CamStatus, string> = { online: 'Online', offline: 'Offline', disabled: 'Disabled' };
export const TYPE_LABEL: Record<CamType, string> = { ptz: 'PTZ', fixed: 'Fixed', unchecked: 'Not checked' };

export function statusOf(c: Camera): CamStatus {
  if (!c.enabled) return 'disabled';
  return c.health_status === 'connected' ? 'online' : 'offline';
}

export function typeOf(c: Camera): CamType {
  if (c.ptz_capable) return 'ptz';
  return c.ptz ? 'fixed' : 'unchecked';
}

export function makerKey(c: Camera): string {
  // Spaces and punctuation don't make a different maker: ONVIF answers "CPPLUS", "CP PLUS" and "Cp-Plus" (KEC has both).
  return (c.vendor || '').toUpperCase().replace(/[^A-Z0-9]+/g, '') || 'UNKNOWN';
}

export function needsOnvifLogin(c: Camera): boolean {
  return !!c.ip && !c.onvif_capable;
}

export function matches(c: Camera, f: CameraFilter): boolean {
  if (f.status && statusOf(c) !== f.status) return false;
  if (f.type && typeOf(c) !== f.type) return false;
  if (f.maker && makerKey(c) !== f.maker) return false;
  if (f.recording && (c.recording ? 'on' : 'off') !== f.recording) return false;
  if (f.attention && !needsOnvifLogin(c)) return false;
  return true;
}

export function isEmpty(f: CameraFilter): boolean {
  return !f.status && !f.type && !f.maker && !f.recording && !f.attention;
}

export function applyFilter(cams: Camera[], f: CameraFilter): Camera[] {
  return isEmpty(f) ? cams : cams.filter(c => matches(c, f));
}

export interface Facets {
  status: Record<CamStatus, number>;
  type: Record<CamType, number>;
  /** key -> {label: the first spelling seen, n} */
  maker: Record<string, { label: string; n: number }>;
  recording: { on: number; off: number };
  attention: number;
}

/** Counts per chip over `cams` (the set before these filters). */
export function facets(cams: Camera[]): Facets {
  const out: Facets = {
    status: { online: 0, offline: 0, disabled: 0 },
    type: { ptz: 0, fixed: 0, unchecked: 0 },
    maker: {},
    recording: { on: 0, off: 0 },
    attention: 0,
  };
  for (const c of cams) {
    out.status[statusOf(c)] += 1;
    out.type[typeOf(c)] += 1;
    const k = makerKey(c);
    const m = (out.maker[k] = out.maker[k] || { label: k === 'UNKNOWN' ? 'Unknown' : (c.vendor || '').trim(), n: 0 });
    m.n += 1;
    out.recording[c.recording ? 'on' : 'off'] += 1;
    if (needsOnvifLogin(c)) out.attention += 1;
  }
  return out;
}

/** Remembered per page in this browser. Never trusted: anything malformed is dropped. */
export function loadFilter(key: string): CameraFilter {
  try {
    const raw = JSON.parse(localStorage.getItem(`vms.camfilter.${key}`) || '{}');
    const f: CameraFilter = {};
    if (raw && typeof raw === 'object') {
      if (raw.status in STATUS_LABEL) f.status = raw.status;
      if (raw.type in TYPE_LABEL) f.type = raw.type;
      if (typeof raw.maker === 'string' && raw.maker) f.maker = raw.maker.slice(0, 128);
      if (raw.recording === 'on' || raw.recording === 'off') f.recording = raw.recording;
      if (raw.attention === true) f.attention = true;
    }
    return f;
  } catch {
    return {};
  }
}

export function saveFilter(key: string, f: CameraFilter): void {
  try {
    localStorage.setItem(`vms.camfilter.${key}`, JSON.stringify(f));
  } catch {
    /* private window / blocked storage: the filter just isn't remembered */
  }
}
