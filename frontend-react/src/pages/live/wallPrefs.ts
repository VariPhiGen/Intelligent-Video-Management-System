/**
 * wallPrefs.ts — video-wall settings = the ADMIN'S DEFAULT (server, /api/branding wall_defaults)
 * overlaid with THIS BROWSER'S OVERRIDES (localStorage), field by field.
 *
 * Why two layers (found in use): with everything per-browser, an admin set up the wall and an
 * operator on another browser saw none of it — a first-visit seed of 16 cameras, no padding, no
 * colours. Now every browser starts from the admin default, and only the fields someone changed on
 * a given screen stay local (a TV whose overlay needs more top padding). Everything NOT overridden
 * keeps following the default, so when the admin changes the cameras next week, every screen that
 * didn't customise its cameras follows. Overrides survive re-login on purpose — TV sessions expire,
 * and a padding fix that vanished at every timeout would have to be redone daily; "Reset" in ⚙
 * (or clearing site data) goes back to the default.
 *
 * Cameras are ONE override unit (followAll + cameraIds together): "all cameras" with a stale
 * private list underneath, or vice versa, is never a state anyone chose.
 *
 * LIVE = the relay has the stream ready (`ready_since`, from MediaMTX), not `health_status`: found
 * on a real site where every camera read "Unknown" while 16 of 18 were streaming.
 */
import type { Camera } from '@/lib/types';

export interface WallPrefs {
  followAll: boolean;       // the wall IS "every enabled camera" — new cameras join without re-picking
  cameraIds: string[];      // the fixed selection when !followAll (wall order)
  hideOffline: boolean;     // drop not-streaming cameras from the grid; header says 12 / 14 live
  topPad: number;           // px of empty space above the header (TV overlays / overscan)
  logoH: number;            // logo height px
  titlePx: number;          // title font size px
  bgLight: string;          // '#rrggbb' behind the wall, per theme — '' = theme default
  bgDark: string;
  titleColorLight: string;  // title colour per theme — '' = theme text colour
  titleColorDark: string;
}
export type WallKey = keyof WallPrefs;
export type Theme = 'light' | 'dark';

export const WALL_PREFS_DEFAULT: WallPrefs = {
  followAll: true, cameraIds: [], hideOffline: true, topPad: 0, logoH: 36, titlePx: 15,
  bgLight: '', bgDark: '', titleColorLight: '', titleColorDark: '',
};
export const WALL_PREF_LIMITS = { topPad: [0, 240], logoH: [20, 160], titlePx: [12, 72] } as const;
const COLOR_KEYS = ['bgLight', 'bgDark', 'titleColorLight', 'titleColorDark'] as const;
const NUM_KEYS = ['topPad', 'logoH', 'titlePx'] as const;

const KEY = 'vms-wall-local';
// Earlier, per-browser-only versions. Their contents are dropped, not migrated: the legacy
// 'vms-wall' was a first-visit seed nobody chose (the operator's "16 cameras"), and carrying either
// over as overrides would pin those browsers away from the admin's default — the very bug fixed here.
const LEGACY_KEYS = ['vms-wall', 'vms-wall-prefs'];

const isColor = (v: unknown): v is string => typeof v === 'string' && (v === '' || /^#[0-9a-f]{6}$/i.test(v));

/** Keep only well-formed fields (clamped / lower-cased); anything else is simply not overridden. */
export function cleanPartial(raw: unknown): Partial<WallPrefs> {
  const r = (raw && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  const out: Partial<WallPrefs> = {};
  if (typeof r.followAll === 'boolean') out.followAll = r.followAll;
  if (Array.isArray(r.cameraIds)) out.cameraIds = r.cameraIds.filter((x): x is string => typeof x === 'string');
  if (typeof r.hideOffline === 'boolean') out.hideOffline = r.hideOffline;
  for (const k of NUM_KEYS) {
    const v = r[k];
    if (typeof v === 'number' && Number.isFinite(v)) {
      const [lo, hi] = WALL_PREF_LIMITS[k];
      out[k] = Math.min(hi, Math.max(lo, Math.round(v)));
    }
  }
  // Only a plain hex colour ever reaches a style attribute.
  for (const k of COLOR_KEYS) if (isColor(r[k])) out[k] = (r[k] as string).toLowerCase();
  return out;
}

export function normalizePrefs(raw: unknown): WallPrefs {
  return { ...WALL_PREFS_DEFAULT, ...cleanPartial(raw) };
}

/** What the screen shows: admin default (or built-in), with this browser's overrides on top. */
export function effectivePrefs(serverDefaults: unknown, local: Partial<WallPrefs>): WallPrefs {
  return { ...normalizePrefs(serverDefaults), ...local };
}

export function loadLocal(): Partial<WallPrefs> {
  try {
    LEGACY_KEYS.forEach(k => localStorage.removeItem(k));
    return cleanPartial(JSON.parse(localStorage.getItem(KEY) || 'null'));
  } catch { return {}; }
}

export function saveLocal(p: Partial<WallPrefs>) {
  try {
    if (Object.keys(p).length) localStorage.setItem(KEY, JSON.stringify(p));
    else localStorage.removeItem(KEY);
  } catch { /* private mode — still applies this session */ }
}

/** Override `patch` on this browser. A field equal to the default is dropped rather than stored,
 *  so a slider dragged back to the default goes back to FOLLOWING the default. The camera pair is
 *  pruned/kept as one unit. */
export function withOverride(local: Partial<WallPrefs>, defaults: WallPrefs, patch: Partial<WallPrefs>): Partial<WallPrefs> {
  const next: Partial<WallPrefs> = { ...local, ...cleanPartial(patch) };
  const same = (k: WallKey) => JSON.stringify(next[k]) === JSON.stringify(defaults[k]);
  for (const k of Object.keys(next) as WallKey[]) {
    if (k !== 'followAll' && k !== 'cameraIds' && same(k)) delete next[k];
  }
  if ('followAll' in next || 'cameraIds' in next) {
    next.followAll = next.followAll ?? defaults.followAll;
    next.cameraIds = next.cameraIds ?? defaults.cameraIds;
    // "all cameras" ignores the list, so two follow-all states are equal whatever the lists say
    const equal = next.followAll === defaults.followAll
      && (next.followAll || JSON.stringify(next.cameraIds) === JSON.stringify(defaults.cameraIds));
    if (equal) { delete next.followAll; delete next.cameraIds; }
  }
  return next;
}

export function withoutOverride(local: Partial<WallPrefs>, keys: WallKey[]): Partial<WallPrefs> {
  const next = { ...local };
  for (const k of keys) delete next[k];
  return next;
}

export const themed = (p: WallPrefs, theme: Theme) => ({
  bg: theme === 'dark' ? p.bgDark : p.bgLight,
  titleColor: theme === 'dark' ? p.titleColorDark : p.titleColorLight,
});

export function isStreaming(c: Camera): boolean {
  return !!c.ready_since || c.health_status === 'connected';
}

/**
 * Hysteresis so a camera that blips for one poll doesn't make the whole grid re-flow twice:
 * a camera must have been seen down continuously for `graceMs` before it leaves the wall, and it
 * comes back the moment it streams again. Cameras already down when the wall first loads are hidden
 * straight away (`downSince` = -Infinity) — no point showing 20 s of black tiles on a fresh page.
 *
 * Safety net: if NOTHING on the wall is streaming, that's the relay (or this browser's view of it)
 * being down, not 14 simultaneous camera failures — show every tile instead of a blank wall.
 */
export function splitWall(cams: Camera[], downSince: Map<string, number>, now: number, graceMs: number,
                          hideOffline: boolean): { shown: Camera[]; offline: Camera[]; liveCount: number; allDown: boolean } {
  const liveCount = cams.filter(isStreaming).length;
  const allDown = cams.length > 0 && liveCount === 0;
  if (!hideOffline || allDown) {
    return { shown: cams, offline: cams.filter(c => !isStreaming(c)), liveCount, allDown };
  }
  const shown: Camera[] = [];
  const offline: Camera[] = [];
  for (const c of cams) {
    const since = downSince.get(c.id);
    if (!isStreaming(c) && since !== undefined && now - since >= graceMs) offline.push(c);
    else shown.push(c);
  }
  return { shown, offline, liveCount, allDown };
}

/** Update the down-since map from a fresh camera list (mutates + returns it). */
export function trackDown(downSince: Map<string, number>, cams: Camera[], now: number, firstLoad: boolean) {
  const ids = new Set(cams.map(c => c.id));
  for (const id of [...downSince.keys()]) if (!ids.has(id)) downSince.delete(id);
  for (const c of cams) {
    if (isStreaming(c)) downSince.delete(c.id);
    else if (!downSince.has(c.id)) downSince.set(c.id, firstLoad ? Number.NEGATIVE_INFINITY : now);
  }
  return downSince;
}

/** "Follow all" order: zone, then name — the same grouping operators see everywhere else. */
export function allWallCameras(cameras: Camera[], zoneOf: (c: Camera) => string): Camera[] {
  return cameras.filter(c => c.enabled)
    .sort((a, b) => zoneOf(a).localeCompare(zoneOf(b)) || (a.name || '').localeCompare(b.name || ''));
}
