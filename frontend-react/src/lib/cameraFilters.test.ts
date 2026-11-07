/**
 * Camera list filters: the facets are read off the camera list alone, and "never
 * checked" PTZ is not reported as "fixed" (the KEC case: cameras whose ONVIF
 * login never worked looked like fixed cameras, which hid why PTZ was missing).
 */
import { describe, expect, it } from 'vitest';
import { applyFilter, facets, loadFilter, makerKey, saveFilter, typeOf } from './cameraFilters';
import type { Camera } from './types';

const cam = (over: Partial<Camera>): Camera => ({
  id: Math.random().toString(36), name: 'Cam', slug: 'cam', enabled: true, recording: true,
  health_status: 'connected', ip: '192.168.0.10', vendor: 'CP PLUS', onvif_capable: true,
  ptz_capable: false, ptz: { supported: false }, ...over,
} as unknown as Camera);

const CAMS = [
  cam({ name: 'PTZ online', ptz_capable: true, ptz: { supported: true } as never }),
  cam({ name: 'Fixed offline', health_status: 'disconnected', vendor: 'CP Plus' }),
  cam({ name: 'No login', onvif_capable: false, ptz: null, vendor: null }),
  cam({ name: 'Disabled', enabled: false, recording: false }),
];

describe('cameraFilters', () => {
  it('keeps "never checked" apart from "fixed"', () => {
    expect(CAMS.map(typeOf)).toEqual(['ptz', 'fixed', 'unchecked', 'fixed']);
  });

  it('groups makers case-insensitively and counts every facet', () => {
    expect(makerKey(CAMS[0])).toBe(makerKey(CAMS[1]));
    expect(makerKey({ vendor: 'CPPLUS' } as Camera)).toBe(makerKey({ vendor: 'Cp-Plus' } as Camera));
    const f = facets(CAMS);
    expect(f.status).toEqual({ online: 2, offline: 1, disabled: 1 });
    expect(f.maker.CPPLUS.n).toBe(3);
    expect(f.maker.UNKNOWN).toEqual({ label: 'Unknown', n: 1 });
    expect(f.attention).toBe(1);
    expect(f.recording).toEqual({ on: 3, off: 1 });
  });

  it('combines facets with AND', () => {
    const names = (f: Parameters<typeof applyFilter>[1]) => applyFilter(CAMS, f).map(c => c.name);
    expect(names({})).toHaveLength(4);
    expect(names({ status: 'online' })).toEqual(['PTZ online', 'No login']);
    expect(names({ status: 'online', type: 'ptz' })).toEqual(['PTZ online']);
    expect(names({ attention: true })).toEqual(['No login']);
    expect(names({ maker: 'CPPLUS', recording: 'off' })).toEqual(['Disabled']);
  });

  it('remembers a filter and drops anything malformed', () => {
    saveFilter('t', { type: 'ptz', attention: true });
    expect(loadFilter('t')).toEqual({ type: 'ptz', attention: true });
    localStorage.setItem('vms.camfilter.t', JSON.stringify({ type: 'robot', status: 'online', attention: 'yes' }));
    expect(loadFilter('t')).toEqual({ status: 'online' });
    localStorage.setItem('vms.camfilter.t', '{not json');
    expect(loadFilter('t')).toEqual({});
  });
});
