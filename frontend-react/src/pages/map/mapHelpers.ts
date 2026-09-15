/**
 * mapHelpers.ts — pure functions behind the Map tab. Kept side-effect free so
 * Tasks 5–6 (HA devices, heatmap) can reuse `placedOn`/`zoneBoxes` without
 * dragging React state along.
 */
import type { Camera } from '@/lib/types';

export interface MapDot { cam: Camera; x: number; y: number }

export const sitemapRef = (c: Camera): { id?: number; x?: number; y?: number } =>
  (c.metadata?.sitemap as any) || {};

/** Cameras placed (with coords) on the given sitemap. */
export const placedOn = (cameras: Camera[], sitemapId: number): MapDot[] =>
  cameras.flatMap(cam => {
    const r = sitemapRef(cam);
    return r.id === sitemapId && r.x != null && r.y != null
      ? [{ cam, x: r.x, y: r.y }] : [];
  });

/** Cameras assigned to the map but lacking coordinates (the "unplaced" tray). */
export const unplacedOn = (cameras: Camera[], sitemapId: number): Camera[] =>
  cameras.filter(c => {
    const r = sitemapRef(c);
    return r.id === sitemapId && (r.x == null || r.y == null);
  });

/**
 * green = healthy, orange = live motion alarm, yellow = any other health
 * issue. `motionState` is `MotionCamera['state']`, keyed by slug — that's
 * the shape `useCameras()` already hands back (`motion[cam.slug]`), so
 * callers don't need to reshape it into a full MotionCamera first.
 */
export const dotColor = (cam: Camera, motionState?: string): string => {
  if (motionState === 'TRIGGERED') return 'var(--orange, #e8963c)';
  if (cam.health_status === 'connected') return 'var(--green)';
  return 'var(--yellow)';
};

// ── GPS georeferencing (Option A) ─────────────────────────────────────────────
//
// A floor plan is just pixels; GPS is lat/lng on Earth. Calibration control
// points ({x,y normalized 0–1} ↔ {lat,lng}) let us fit a transform so a
// camera's GPS auto-places on the plan. 2 points → similarity (rotation +
// uniform scale + translation, north-up); 3 → full affine (also shear, for a
// tilted/not-to-scale plan). Everything here is pure and offline — no tiles.

export interface CalPoint { x: number; y: number; lat: number; lng: number }
export interface LatLng { lat: number; lng: number }

/** Tolerant "lat, lng" parser: accepts "28.61, 77.21", "28.61 77.21",
 * degree signs and N/S/E/W suffixes. Returns null on anything unparseable or
 * out of range — an invalid value simply means "no auto-placement", never an
 * error. */
export function parseGps(raw: string | null | undefined): LatLng | null {
  if (!raw) return null;
  const cleaned = String(raw).trim().replace(/°/g, ' ');
  const parts = (cleaned.includes(',') ? cleaned.split(',') : cleaned.split(/\s+/))
    .map(s => s.trim()).filter(Boolean);
  if (parts.length !== 2) return null;
  const num = (tok: string): number | null => {
    const m = tok.match(/^([+-]?\d+(?:\.\d+)?)\s*([NSEWnsew])?$/);
    if (!m) return null;
    let v = parseFloat(m[1]);
    const h = m[2]?.toUpperCase();
    if (h === 'S' || h === 'W') v = -Math.abs(v);
    else if (h === 'N' || h === 'E') v = Math.abs(v);
    return v;
  };
  const lat = num(parts[0]);
  const lng = num(parts[1]);
  if (lat == null || lng == null) return null;
  if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
  return { lat, lng };
}

function det3(m: number[][]): number {
  return (
    m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) -
    m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) +
    m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
  );
}

/** Cramer's rule for a 3×3 system; null if singular (collinear points). */
function solve3(M: number[][], r: number[]): number[] | null {
  const D = det3(M);
  if (Math.abs(D) < 1e-12) return null;
  const col = (i: number) => M.map((row, ri) => row.map((v, ci) => (ci === i ? r[ri] : v)));
  return [det3(col(0)) / D, det3(col(1)) / D, det3(col(2)) / D];
}

/** Fit lat/lng → normalized {x,y} from 2 or 3 control points, or null if the
 * points are missing/degenerate. Longitude is scaled by cos(latitude) so east
 * and north are metrically comparable before the fit. */
export function buildGeoTransform(
  points: CalPoint[] | null | undefined,
): ((lat: number, lng: number) => { x: number; y: number }) | null {
  if (!points || (points.length !== 2 && points.length !== 3)) return null;
  const lat0 = points.reduce((s, p) => s + p.lat, 0) / points.length;
  const k = Math.cos((lat0 * Math.PI) / 180);
  const proj = (lat: number, lng: number) => ({ E: lng * k, N: lat });
  const P = points.map(p => ({ ...proj(p.lat, p.lng), x: p.x, y: p.y }));

  if (points.length === 2) {
    const [p1, p2] = P;
    const dE = p2.E - p1.E, dN = p2.N - p1.N, dx = p2.x - p1.x, dy = p2.y - p1.y;
    const det = dE * dE + dN * dN;
    if (Math.abs(det) < 1e-12) return null;
    const a = (dx * dE + dy * dN) / det;
    const b = (dy * dE - dx * dN) / det;
    const c = p1.x - a * p1.E + b * p1.N;
    const d = p1.y - b * p1.E - a * p1.N;
    return (lat, lng) => {
      const { E, N } = proj(lat, lng);
      return { x: a * E - b * N + c, y: b * E + a * N + d };
    };
  }

  const M = P.map(p => [p.E, p.N, 1]);
  const sx = solve3(M, P.map(p => p.x));
  const sy = solve3(M, P.map(p => p.y));
  if (!sx || !sy) return null;
  return (lat, lng) => {
    const { E, N } = proj(lat, lng);
    return { x: sx[0] * E + sx[1] * N + sx[2], y: sy[0] * E + sy[1] * N + sy[2] };
  };
}

export interface ResolvedDot extends MapDot { auto: boolean }

/** Dots to render for a map: a manual placement on THIS map always wins;
 * otherwise, if the map is calibrated and the camera has GPS that projects
 * inside the plan, an auto-placed dot. Cameras with neither don't appear
 * (they sit in the unplaced tray). */
export function resolveDots(
  cameras: Camera[], sitemapId: number, calibration?: CalPoint[] | null,
): ResolvedDot[] {
  const tf = buildGeoTransform(calibration);
  const out: ResolvedDot[] = [];
  const margin = 0.03;
  for (const cam of cameras) {
    const r = sitemapRef(cam);
    if (r.id === sitemapId && r.x != null && r.y != null) {
      out.push({ cam, x: r.x, y: r.y, auto: false });
      continue;
    }
    if (tf) {
      const g = parseGps(cam.metadata?.gps);
      if (g) {
        const p = tf(g.lat, g.lng);
        if (p.x >= -margin && p.x <= 1 + margin && p.y >= -margin && p.y <= 1 + margin) {
          out.push({
            cam,
            x: Math.min(1, Math.max(0, p.x)),
            y: Math.min(1, Math.max(0, p.y)),
            auto: true,
          });
        }
      }
    }
  }
  return out;
}

export interface ZoneBox { zone: string; x: number; y: number; w: number; h: number }

/** Padded bounding box per zone with ≥2 placed cameras — the mock's zone
 * rectangles, computed rather than stored. All values normalized 0–1. */
export function zoneBoxes(dots: MapDot[], pad = 0.045): ZoneBox[] {
  const byZone = new Map<string, MapDot[]>();
  for (const d of dots) {
    const z = (d.cam.metadata?.zone as string) || '';
    if (!z) continue;
    byZone.set(z, [...(byZone.get(z) || []), d]);
  }
  const boxes: ZoneBox[] = [];
  for (const [zone, ds] of byZone) {
    if (ds.length < 2) continue;
    const xs = ds.map(d => d.x), ys = ds.map(d => d.y);
    const x = Math.max(0, Math.min(...xs) - pad);
    const y = Math.max(0, Math.min(...ys) - pad);
    boxes.push({
      zone, x, y,
      w: Math.min(1, Math.max(...xs) + pad) - x,
      h: Math.min(1, Math.max(...ys) + pad) - y,
    });
  }
  return boxes.sort((a, b) => a.zone.localeCompare(b.zone));
}
