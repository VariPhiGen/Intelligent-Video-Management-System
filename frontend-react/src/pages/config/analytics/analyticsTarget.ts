/**
 * analyticsTarget.ts — WHERE the Zones & AI tabs read and save a config: the whole camera, or one preset of a PTZ
 * camera (each preset is a fixed-camera view with its own zones and rules — backend services/ptz_views). The tabs
 * stay the same; only the target changes: what to load fresh before a save, where to PUT, and which picture to
 * draw on (a preset's own reference picture, not wherever the camera happens to point now).
 */
import { apiFetch } from '@/lib/api';
import type { AnalyticsConfig, Camera, PtzPreset } from '@/lib/types';

export interface AnalyticsTarget {
  key: string;
  label: string;
  /** The stored config, read fresh (a save merges over it). */
  load(): Promise<AnalyticsConfig>;
  save(cfg: AnalyticsConfig): Promise<void>;
  /** The picture the editor draws over. */
  snapshotPath: string;
  preset?: PtzPreset;
}

const EMPTY: AnalyticsConfig = { regions: {}, activities: [] };

export function cameraTarget(camera: Camera): AnalyticsTarget {
  return {
    key: 'camera', label: camera.name || camera.slug,
    load: async () => (await apiFetch<Camera>(`/cameras/${camera.id}`)).analytics_config || EMPTY,
    save: async cfg => { await apiFetch(`/cameras/${camera.id}/analytics`, { method: 'PUT', body: JSON.stringify(cfg) }); },
    snapshotPath: `/cameras/${camera.id}/snapshot`,
  };
}

export function presetTarget(camera: Camera, preset: PtzPreset, night = false): AnalyticsTarget {
  const base = `/cameras/${camera.id}/ptz/presets/${encodeURIComponent(preset.token)}`;
  return {
    key: `preset:${preset.id || preset.token}`, label: preset.name || `Preset ${preset.token}`, preset,
    load: async () => (await apiFetch<{ analytics_config: AnalyticsConfig }>(`${base}/analytics`)).analytics_config || EMPTY,
    save: async cfg => { await apiFetch(`${base}/analytics`, { method: 'PUT', body: JSON.stringify(cfg) }); },
    snapshotPath: `${base}/reference${night ? '?night=1' : ''}`,
  };
}

/** A preset's counts for the picker / list: "2 zones · 1 rule". */
export function aiText(p: Pick<PtzPreset, 'ai'>): string {
  const a = p.ai;
  if (!a || (!a.zones && !a.tripwires && !a.rules)) return 'no AI';
  const parts = [];
  if (a.zones) parts.push(`${a.zones} zone${a.zones === 1 ? '' : 's'}`);
  if (a.tripwires) parts.push(`${a.tripwires} tripwire${a.tripwires === 1 ? '' : 's'}`);
  parts.push(`${a.rules} rule${a.rules === 1 ? '' : 's'}`);
  return parts.join(' · ');
}

/** Does this camera's AI follow its presets? (any preset with a rule — the backend's rule) */
export const followsPresets = (presets: PtzPreset[] | null | undefined) => (presets || []).some(p => (p.ai?.rules || 0) > 0);

/** The view moved under this preset's zones: a short flag for lists ("check zones" — its rules are paused). */
export function reviewText(p: Pick<PtzPreset, 'review'>): string | null {
  const r = p.review;
  if (!r) return null;
  if (r.state === 'awaiting_picture') return 'check zones — waiting for a picture of the new view';
  return r.paused ? 'check zones — rules paused' : 'zones moved with the view';
}
