/** Zones / AI tabs edit the whole camera or one PTZ preset (each preset = a fixed-camera view). */
import { describe, expect, it, vi } from 'vitest';

const calls: [string, any][] = [];
vi.mock('@/lib/api', () => ({
  apiFetch: vi.fn(async (path: string, opts?: any) => {
    calls.push([path, opts]);
    return path.endsWith('/analytics') && !opts ? { analytics_config: { regions: {}, activities: [{ type: 'x', regions: [] }] } }
      : { analytics_config: { regions: {}, activities: [] } };
  }),
}));

import { aiText, cameraTarget, followsPresets, presetTarget, reviewText } from './analyticsTarget';

const cam = { id: 'c1', slug: 'post-a', name: 'Post A' } as any;
const gate = { id: 'p1', token: '3', name: 'Gate', pan: 0, tilt: 0, zoom: 0, ai: { zones: 2, tripwires: 0, rules: 1 } } as any;

describe('analyticsTarget', () => {
  it('a preset is read, saved and drawn on through its own routes and picture', async () => {
    const t = presetTarget(cam, gate);
    expect(t.snapshotPath).toBe('/cameras/c1/ptz/presets/3/reference');
    expect(presetTarget(cam, gate, true).snapshotPath).toBe('/cameras/c1/ptz/presets/3/reference?night=1');
    expect((await t.load()).activities).toHaveLength(1);
    await t.save({ regions: {}, activities: [] });
    expect(calls.at(-1)).toEqual(['/cameras/c1/ptz/presets/3/analytics', expect.objectContaining({ method: 'PUT' })]);
  });

  it('the whole camera keeps its old routes', async () => {
    const t = cameraTarget(cam);
    expect(t.snapshotPath).toBe('/cameras/c1/snapshot');
    await t.save({ regions: {}, activities: [] });
    expect(calls.at(-1)?.[0]).toBe('/cameras/c1/analytics');
  });

  it('says what each preset watches and whether the camera follows its presets', () => {
    expect(aiText(gate)).toBe('2 zones · 1 rule');
    expect(aiText({ ai: { zones: 0, tripwires: 1, rules: 0 } })).toBe('1 tripwire · 0 rules');
    expect(aiText({})).toBe('no AI');
    expect(followsPresets([gate])).toBe(true);
    expect(followsPresets([{ ...gate, ai: { zones: 1, tripwires: 0, rules: 0 } }])).toBe(false);
  });

  it('flags a preset whose view moved under its zones', () => {
    const r = { note: '', reason: '', by: null, at: '', offset: null, zoom_changed: false, can_undo: false };
    expect(reviewText({})).toBeNull();
    expect(reviewText({ review: { ...r, state: 'shifted', paused: false } })).toBe('zones moved with the view');
    expect(reviewText({ review: { ...r, state: 'review', paused: true } })).toBe('check zones — rules paused');
    expect(reviewText({ review: { ...r, state: 'awaiting_picture', paused: true } })).toContain('waiting for a picture');
  });
});
