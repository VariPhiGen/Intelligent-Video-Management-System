/**
 * analyticsConfig — what AI Config saves, now that each activity's zone rule and
 * settings come from the analytics engine's definition.
 *
 * The rule that matters most: a zone-less activity is saved only if its
 * definition says "no zone = whole frame", and an activity whose zones were
 * deleted is dropped — never silently widened to the whole frame.
 */
import { describe, expect, it } from 'vitest';
import type { AnalyticsConfig } from '@/lib/types';
import type { ActivityMeta } from './activities';
import { buildActivitySave, buildRegionSave, defaultParams } from './analyticsConfig';

const ZONE = { kind: 'zone' as const, name: 'Zone 1', color: '#4fd1c5', points: [[0, 0], [1, 0], [1, 1]] };

const byKey: Record<string, ActivityMeta> = {
  people_gathering: {
    key: 'people_gathering', label: 'People gathering', color: '#fff', status: 'available', zone_rule: 'optional',
    params_schema: [
      { key: 'required_duration_s', label: 'Gathering duration', kind: 'number', default: 60, unit: 's' },
      { key: 'last_time', label: 'Reset after no gathering', kind: 'number', default: 30, unit: 's' },
      { key: 'min_group_size', label: 'Minimum people', kind: 'number', default: 2, min: 2, max: 50, integer: true },
      { key: 'proximity_factor', label: 'Proximity factor', kind: 'number', default: 1.35, unit: '× box width', min: 0.5, max: 10 },
      { key: 'cooldown_s', label: 'Cooldown', kind: 'number', default: 100, unit: 's', min: 1, max: 86400 },
    ],
  },
  stray_parking: {
    key: 'stray_parking', label: 'Stray parking', color: '#fff', status: 'available', zone_rule: 'required',
    params_schema: [
      { key: 'vehicle_classes', label: 'Vehicle types', kind: 'list', default: ['car', 'truck'],
        options: ['car', 'motorcycle', 'bus', 'truck'], min_items: 1 },
      { key: 'required_duration_s', label: 'Required parking duration', kind: 'number', default: 600, unit: 's' },
    ],
  },
};

const schedule = { active_hours: null, active_days: ['Mon'] };

describe('defaultParams', () => {
  it('seeds the definition defaults and copies lists rather than sharing them', () => {
    const p = defaultParams(byKey.stray_parking);
    expect(p.required_duration_s).toBe(600);
    expect(p.vehicle_classes).toEqual(['car', 'truck']);
    expect(p.vehicle_classes).not.toBe(byKey.stray_parking.params_schema![0].default);
  });

  it('does not seed a setting the engine does not offer', () => {
    const meta: ActivityMeta = { ...byKey.people_gathering, params_schema: [
      { key: 'hidden', label: 'Hidden', kind: 'number', default: 1, configurable: false }] };
    expect('hidden' in defaultParams(meta)).toBe(false);
  });
});

describe('buildActivitySave', () => {
  const stored: AnalyticsConfig = { regions: { region_1: ZONE }, activities: [] };

  it('keeps a zone-less activity whose definition watches the whole frame', () => {
    const out = buildActivitySave(stored, [{ type: 'people_gathering', regions: [], params: { ...schedule } }], byKey);
    expect(out.activities).toEqual([{ type: 'people_gathering', regions: [], params: schedule }]);
  });

  it('drops a zone-less activity whose definition needs a zone', () => {
    const out = buildActivitySave(stored, [{ type: 'stray_parking', regions: [], params: { ...schedule } }], byKey);
    expect(out.activities).toEqual([]);
  });

  it('never widens an activity whose zones were deleted', () => {
    const out = buildActivitySave(stored, [{ type: 'people_gathering', regions: ['gone'], params: { ...schedule } }], byKey);
    expect(out.activities).toEqual([]);
  });

  it('saves only the options the engine supports for a list setting', () => {
    const out = buildActivitySave(stored, [{
      type: 'stray_parking', regions: ['region_1'],
      params: { ...schedule, vehicle_classes: ['car', 'bike', 'other_moving_machinary'], required_frames: 2400 },
    }], byKey);
    expect(out.activities[0].params).toEqual({ ...schedule, vehicle_classes: ['car'] });
  });
});

describe('People Gathering behaviour settings', () => {
  it('are seeded with the developer defaults when the activity is added', () => {
    const p = defaultParams(byKey.people_gathering);
    expect([p.required_duration_s, p.last_time, p.min_group_size, p.proximity_factor, p.cooldown_s])
      .toEqual([60, 30, 2, 1.35, 100]);
  });

  it('are saved with the camera, and retired DeepStream keys are not', () => {
    const stored: AnalyticsConfig = { regions: { region_1: ZONE }, activities: [] };
    const params = { ...schedule, required_duration_s: 120, last_time: 30,
                     min_group_size: 5, proximity_factor: 2, cooldown_s: 300 };
    const out = buildActivitySave(stored, [{
      type: 'people_gathering', regions: ['region_1'],
      params: { ...params, person_limit: 4, frame_accuracy: 1800 },
    }], byKey);
    expect(out.activities[0].params).toEqual(params);
  });

  it('are not written for a camera that never set them', () => {
    const stored: AnalyticsConfig = { regions: { region_1: ZONE }, activities: [] };
    const params = { ...schedule, required_duration_s: 60, last_time: 30 };
    const out = buildActivitySave(stored, [{ type: 'people_gathering', regions: ['region_1'], params }], byKey);
    expect(out.activities[0].params).toEqual(params);
  });
});

describe('buildRegionSave', () => {
  it('drops an activity that lost every zone but keeps a whole-frame one', () => {
    const stored: AnalyticsConfig = {
      regions: { region_1: ZONE },
      activities: [
        { type: 'stray_parking', regions: ['region_1'], params: {} },
        { type: 'people_gathering', regions: [], params: {} },
      ],
    };
    const out = buildRegionSave(stored, {}, []);
    expect(out.activities.map(a => a.type)).toEqual(['people_gathering']);
    expect(out.activities[0]).toEqual({ type: 'people_gathering', regions: [], params: {} });
  });
});
