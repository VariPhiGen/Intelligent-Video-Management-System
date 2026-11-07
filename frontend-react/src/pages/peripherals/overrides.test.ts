import { describe, expect, it } from 'vitest';

import { overridesFor } from './StatusPanel';
import type { Device } from './peripheralsData';

function dev(over: Partial<Device>): Device {
  return {
    id: 'relay-1-ch1', name: 'Relay 1 · CH1', location: 'PoE switch', category: 'Access control',
    enabled: true, placements: 0, lastState: 'off', lastSeen: new Date().toISOString(),
    bindingVerified: true, binding: null, glyph: '⊟', state: 'OFF', tone: 'green', ...over,
  } as Device;
}

describe('manual override buttons', () => {
  it('access control always offers both Open and Close', () => {
    const pair = overridesFor(dev({}));
    expect(pair.map(p => [p.label, p.command])).toEqual([['Open', 'open'], ['Close', 'close']]);
  });

  it('a relay reporting on/off marks the matching half', () => {
    expect(overridesFor(dev({ lastState: 'on', state: 'ON' })).map(p => p.active)).toEqual([true, false]);
    expect(overridesFor(dev({ lastState: 'off', state: 'OFF' })).map(p => p.active)).toEqual([false, true]);
  });

  it('lighting and audio offer Turn on and Turn off', () => {
    for (const category of ['Lighting', 'Audio · Sensors'] as const) {
      expect(overridesFor(dev({ category })).map(p => p.command)).toEqual(['on', 'off']);
    }
  });

  it('an unknown state marks neither, but both stay offered', () => {
    const pair = overridesFor(dev({ lastState: null, state: 'UNKNOWN' }));
    expect(pair).toHaveLength(2);
    expect(pair.every(p => !p.active)).toBe(true);
  });
});

const ha = (address: string): Partial<Device> => ({
  binding: { driver: 'homeassistant', address, verified_at: '2026-10-27T00:00:00Z' },
});

describe('manual override buttons for Home Assistant devices', () => {
  it('a lock offers Unlock and Lock, whatever its category', () => {
    const pair = overridesFor(dev({ ...ha('lock.side_door'), category: 'Lighting',
                                    lastState: 'unlocked', state: 'UNLOCKED' }));
    expect(pair.map(p => p.command)).toEqual(['unlock', 'lock']);
    expect(pair.map(p => p.active)).toEqual([true, false]);
  });

  it('a relay switch under Access control keeps Open and Close', () => {
    expect(overridesFor(dev(ha('switch.relay_ch1'))).map(p => p.command))
      .toEqual(['open', 'close']);
  });

  it('a scene is one Run button, never marked', () => {
    const pair = overridesFor(dev({ ...ha('scene.lockdown'), category: 'Automation',
                                    lastState: 'ready', state: 'READY' }));
    expect(pair).toEqual([{ label: 'Run', command: 'run', active: false }]);
  });

  it('a button is one Press button', () => {
    expect(overridesFor(dev({ ...ha('button.chime'), category: 'Automation' })))
      .toEqual([{ label: 'Press', command: 'press', active: false }]);
  });

  it('a valve opens and closes', () => {
    expect(overridesFor(dev({ ...ha('valve.sprinkler'), category: 'Climate · Utilities' }))
      .map(p => p.command)).toEqual(['open', 'close']);
  });
});
