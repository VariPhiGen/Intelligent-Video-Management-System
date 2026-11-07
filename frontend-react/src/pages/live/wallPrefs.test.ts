import { describe, expect, it } from 'vitest';
import type { Camera } from '@/lib/types';
import { allWallCameras, effectivePrefs, isStreaming, normalizePrefs, splitWall, themed, trackDown, WALL_PREFS_DEFAULT,
         withOverride, withoutOverride } from './wallPrefs';

const cam = (id: string, live: boolean, extra: Partial<Camera> = {}) =>
  ({ id, name: id, slug: id, enabled: true, health_status: 'unknown', ready_since: live ? '2026-10-04T00:00:00Z' : null,
     metadata: {}, ...extra } as unknown as Camera);

describe('isStreaming', () => {
  it('trusts the relay even when health_status says unknown', () => {
    expect(isStreaming(cam('a', true))).toBe(true);
    expect(isStreaming(cam('b', false))).toBe(false);
    expect(isStreaming(cam('c', false, { health_status: 'connected' }))).toBe(true);
  });
});

describe('splitWall', () => {
  const cams = [cam('a', true), cam('b', false), cam('c', true)];

  it('cameras already down on first load are hidden straight away; header counts 2 / 3', () => {
    const down = trackDown(new Map(), cams, 1000, true);
    const r = splitWall(cams, down, 1000, 20_000, true);
    expect(r.shown.map(c => c.id)).toEqual(['a', 'c']);
    expect(r.offline.map(c => c.id)).toEqual(['b']);
    expect(r.liveCount).toBe(2);
  });

  it('a camera that drops later stays for the grace period, then leaves; comes back at once', () => {
    const down = trackDown(new Map(), cams.map(c => cam(c.id, true)), 0, true);
    const dropped = [cam('a', false), cam('b', true), cam('c', true)];
    trackDown(down, dropped, 5_000, false);
    expect(splitWall(dropped, down, 10_000, 20_000, true).shown).toHaveLength(3);   // blip: still shown
    expect(splitWall(dropped, down, 26_000, 20_000, true).offline.map(c => c.id)).toEqual(['a']);
    const back = [cam('a', true), cam('b', true), cam('c', true)];
    trackDown(down, back, 30_000, false);
    expect(splitWall(back, down, 30_000, 20_000, true).shown).toHaveLength(3);
  });

  it('nothing streaming = relay down: show every tile rather than a blank wall', () => {
    const dead = [cam('a', false), cam('b', false)];
    const r = splitWall(dead, trackDown(new Map(), dead, 0, true), 0, 20_000, true);
    expect(r.allDown).toBe(true);
    expect(r.shown).toHaveLength(2);
  });

  it('hideOffline off keeps every tile', () => {
    const down = trackDown(new Map(), cams, 0, true);
    expect(splitWall(cams, down, 99_000, 20_000, false).shown).toHaveLength(3);
  });
});

describe('prefs', () => {
  it('fills defaults and clamps junk', () => {
    expect(normalizePrefs(null)).toEqual(WALL_PREFS_DEFAULT);
    expect(normalizePrefs({ topPad: 9999, logoH: -5, titlePx: 'big', hideOffline: false }))
      .toMatchObject({ topPad: 240, logoH: 20, titlePx: WALL_PREFS_DEFAULT.titlePx, hideOffline: false });
    expect(normalizePrefs({ bgDark: '#0A0B0C' }).bgDark).toBe('#0a0b0c');
    expect(normalizePrefs({ bgLight: 'red;background:url(x)' }).bgLight).toBe('');
  });
});

describe('admin default + per-browser overrides', () => {
  const server = { followAll: false, cameraIds: ['a', 'b'], topPad: 60, bgDark: '#0b1f3a' };
  const defaults = normalizePrefs(server);

  it('a fresh browser shows exactly the admin default', () => {
    expect(effectivePrefs(server, {})).toEqual(defaults);
  });

  it('overriding one field keeps following the default for everything else', () => {
    const local = withOverride({}, defaults, { topPad: 120 });
    expect(local).toEqual({ topPad: 120 });
    // admin later changes the cameras + colour: this screen follows, keeps only its own padding
    const eff = effectivePrefs({ ...server, cameraIds: ['c'], bgDark: '#000000' }, local);
    expect(eff.cameraIds).toEqual(['c']);
    expect(eff.bgDark).toBe('#000000');
    expect(eff.topPad).toBe(120);
  });

  it('moving a control back to the default stops overriding it', () => {
    const local = withOverride({ topPad: 120 }, defaults, { topPad: 60 });
    expect(local).toEqual({});
  });

  it('cameras are one unit: picking cameras overrides both fields; matching the default drops both', () => {
    const local = withOverride({}, defaults, { followAll: true, cameraIds: [] });
    expect(local).toEqual({ followAll: true, cameraIds: [] });
    expect(withOverride(local, defaults, { followAll: false, cameraIds: ['a', 'b'] })).toEqual({});
    expect(withoutOverride({ topPad: 1, followAll: true, cameraIds: [] }, ['followAll', 'cameraIds'])).toEqual({ topPad: 1 });
  });

  it('colours are per theme', () => {
    const p = effectivePrefs(server, withOverride({}, defaults, { titleColorLight: '#111827' }));
    expect(themed(p, 'dark')).toEqual({ bg: '#0b1f3a', titleColor: '' });
    expect(themed(p, 'light')).toEqual({ bg: '', titleColor: '#111827' });
  });
});

describe('allWallCameras', () => {
  it('every enabled camera, zone then name', () => {
    const z = (c: Camera) => (c.metadata as any).zone || 'Unzoned';
    const list = [cam('y', true, { metadata: { zone: 'B' } } as any), cam('x', true, { metadata: { zone: 'A' } } as any),
                  cam('off', true, { enabled: false } as any)];
    expect(allWallCameras(list, z).map(c => c.id)).toEqual(['x', 'y']);
  });
});
