import { describe, expect, it } from 'vitest';

import {
  actionsFor, actionsForDevice, haDomain, labelForCommand, needsMedia,
  type Device,
} from './peripheralsData';

function dev(over: Partial<Device>): Device {
  return {
    id: 'd', name: 'D', location: '—', category: 'Access control', enabled: true,
    placements: 0, bindingVerified: true, binding: null, glyph: '⊟', state: 'OFF',
    tone: 'green', ...over,
  } as Device;
}
const bound = (driver: 'homeassistant' | 'webhook' | 'mqtt', address: string) =>
  ({ binding: { driver, address } });

describe('haDomain', () => {
  it('is the part before the dot for an HA binding', () => {
    expect(haDomain(dev(bound('homeassistant', 'media_player.lobby')))).toBe('media_player');
  });
  it('is null for any other driver, or no binding', () => {
    expect(haDomain(dev(bound('webhook', 'http://127.0.0.1:8099/relay/x/1')))).toBeNull();
    expect(haDomain(dev({}))).toBeNull();
  });
});

describe('actionsForDevice', () => {
  it('a Home Assistant device gets its domain\'s actions, not its category\'s', () => {
    // Filed under Lighting by mistake: "Flash" would refuse on every fire.
    const lock = dev({ category: 'Lighting', ...bound('homeassistant', 'lock.side_door') });
    expect(actionsForDevice(lock).map(a => a.command)).toEqual(['unlock', 'lock']);
  });

  it('every new HA device type has actions', () => {
    for (const [addr, first] of [
      ['media_player.lobby', 'announce'], ['scene.lockdown', 'run'],
      ['script.evacuate', 'run'], ['button.chime', 'press'],
      ['fan.server_room', 'on'], ['valve.sprinkler', 'open'],
    ] as const) {
      expect(actionsForDevice(dev(bound('homeassistant', addr)))[0].command).toBe(first);
    }
  });

  it('an HA switch uses its category, because a relay does what is wired', () => {
    const gate = dev(bound('homeassistant', 'switch.relay_ch1'));
    expect(actionsForDevice(gate)).toEqual(actionsFor('Access control'));
  });

  it('a gateway device is unchanged: by category', () => {
    const siren = dev({ category: 'Audio · Sensors', ...bound('webhook', 'http://x/relay/h/8') });
    expect(actionsForDevice(siren)).toEqual(actionsFor('Audio · Sensors'));
  });

  it('the new categories have actions for unbound devices too', () => {
    expect(actionsFor('Automation').map(a => a.command)).toEqual(['run']);
    expect(actionsFor('Climate · Utilities').map(a => a.command)).toEqual(['on', 'off']);
  });
});

describe('needsMedia', () => {
  it('an HA speaker announcement must be told what to play', () => {
    expect(needsMedia(dev(bound('homeassistant', 'media_player.lobby')), 'announce')).toBe(true);
    expect(needsMedia(dev(bound('homeassistant', 'media_player.lobby')), 'stop')).toBe(false);
  });
  it('the gateway speaker plays its own configured sound', () => {
    expect(needsMedia(dev(bound('webhook', 'http://x/speaker/239.1.1.1:10000')), 'announce'))
      .toBe(false);
  });
});

describe('labelForCommand', () => {
  it('names the new commands in English', () => {
    expect(['run', 'press', 'stop'].map(labelForCommand)).toEqual(['Run', 'Press', 'Stop']);
  });
});
