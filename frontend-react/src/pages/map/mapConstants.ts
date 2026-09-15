/**
 * mapConstants.ts — values shared by the Map tab's pieces.
 *
 * Split out of MapPage so the layer components, the modals and the hooks can
 * agree on them without importing the page (which would be a cycle).
 */

/** Which layer the plan is showing. Geofences is a disabled chip until that
 *  surface exists. */
export type View = 'cameras' | 'ha' | 'heatmap' | 'geofences';

export const STATUS_BADGE: Record<string, string> = {
  connected: 'badge-green',
  disconnected: 'badge-red',
  error: 'badge-red',
  disabled: 'badge-gray',
  unknown: 'badge-yellow',
};

/** How far the pointer must travel before a press on a dot or pin counts as a
 *  drag rather than a select. Below this, pointerup persists nothing — without
 *  it a plain click rewrote the marker to the cursor position. */
export const DRAG_SLOP_PX = 4;

// ── Upload limits ────────────────────────────────────────────────────────────
// Mirrors the server's guard (backend/models.py — validate_sitemap_dimensions).
// The server is the authority; these run in the browser so a bad plan is
// refused in the file picker instead of after a 10 MB upload.
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const MIN_EDGE_PX = 600;
export const MAX_EDGE_PX = 12_000;
export const MAX_PIXELS = 40_000_000;
export const MAX_ASPECT = 8;

export function checkPlanDimensions(w: number, h: number): string | null {
  if (!w || !h) return 'Could not read the image dimensions';
  const longest = Math.max(w, h), shortest = Math.min(w, h);
  if (longest < MIN_EDGE_PX)
    return `Too small (${w}×${h}). A site plan needs at least ${MIN_EDGE_PX}px on its longest side to stay readable when zoomed in.`;
  if (longest > MAX_EDGE_PX)
    return `Too large (${w}×${h}). Keep each side under ${MAX_EDGE_PX}px — bigger plans exhaust memory in the browser.`;
  if (w * h > MAX_PIXELS)
    return `Too many pixels (${Math.round(w * h / 1e6)} MP). Keep it under ${MAX_PIXELS / 1e6} MP — bigger plans exhaust memory in the browser.`;
  if (longest / shortest > MAX_ASPECT)
    return `Too elongated (${w}×${h}). A plan wider or taller than ${MAX_ASPECT}:1 renders as a thin strip — crop it or split it across two plans.`;
  return null;
}
