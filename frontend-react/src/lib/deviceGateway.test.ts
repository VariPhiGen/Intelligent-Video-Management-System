import { describe, expect, it } from 'vitest';

import { bindingPayload } from './bridge';
import { buildGatewayUrl, healthOf, parseGatewayUrl } from './deviceGateway';

const NOW = new Date('2026-10-15T10:00:00Z');
const RELAY = 'http://127.0.0.1:8099/relay/192.168.1.200/8';
const SPEAKER = 'http://127.0.0.1:8099/speaker/239.1.1.10:10000?probe=192.168.1.102';

describe('gateway URLs', () => {
  it('builds a relay channel URL, leaving default port and slave out', () => {
    expect(buildGatewayUrl('http://127.0.0.1:8099/', {
      kind: 'relay', host: '192.168.1.200', port: 4196, channel: 8, slave: 1,
    })).toBe(RELAY);
    expect(buildGatewayUrl('http://gw:8099', {
      kind: 'relay', host: '10.0.0.5', port: 502, channel: 3, slave: 7,
    })).toBe('http://gw:8099/relay/10.0.0.5:502/3?slave=7');
  });

  it('builds a speaker URL with only the options given', () => {
    expect(buildGatewayUrl('', { kind: 'speaker', group: '239.1.1.10', port: 10000,
                                 probe: '192.168.1.102' })).toBe(SPEAKER);
  });

  it('round-trips, so editing a binding refills the form', () => {
    for (const url of [RELAY, SPEAKER, 'http://gw:8099/relay/10.0.0.5:502/3?slave=7']) {
      const p = parseGatewayUrl(url)!;
      expect(buildGatewayUrl(p.base, p.target)).toBe(url);
    }
  });

  it('is not fooled by any other URL', () => {
    expect(parseGatewayUrl('http://10.0.0.9/relay/1')).toBeNull();
    expect(parseGatewayUrl('not a url')).toBeNull();
  });
});

describe('device health on the wall', () => {
  const gw = (over: Record<string, unknown> = {}) => ({
    driver: 'webhook' as const, address: RELAY, config: { health: true },
    verified_at: '2026-10-15T09:59:48Z', verify_error: null, ...over,
  });

  it('healthy, with the protocol and when it was checked', () => {
    expect(healthOf(gw(), NOW)).toEqual({ tone: 'green', label: 'Healthy · Modbus TCP relay',
                                          detail: 'checked 12s ago' });
  });

  it('unhealthy names the protocol and the reason', () => {
    const h = healthOf(gw({ verify_error: 'unhealthy:modbus-tcp/timeout' }), NOW)!;
    expect(h.tone).toBe('red');
    expect(h.label).toBe('Unhealthy · Modbus TCP relay');
    expect(h.detail).toMatch(/^modbus-tcp\/timeout · last healthy 12s ago$/);
  });

  it('a speaker says so', () => {
    expect(healthOf(gw({ address: SPEAKER }), NOW)!.label).toBe('Healthy · Multicast speaker');
  });

  it('pending before the first check', () => {
    expect(healthOf(gw({ verified_at: null }), NOW)!.tone).toBe('dim');
  });

  it('a binding that is not health-checked never claims the device is healthy', () => {
    const h = healthOf({ driver: 'mqtt', address: 'vms/x/set', verified_at: NOW.toISOString() }, NOW)!;
    expect(h.tone).toBe('dim');
    expect(h.label).toBe('Address OK · mqtt');
  });

  it('nothing for an unbound device', () => {
    expect(healthOf(null, NOW)).toBeNull();
  });
});

describe('bindingPayload', () => {
  it('sends the config, so health can be switched on and off', () => {
    expect(bindingPayload({ driver: 'webhook', address: RELAY, config: { health: true } }).config)
      .toEqual({ health: true });
    expect(bindingPayload({ driver: 'mqtt', address: 'a/b' }).config).toEqual({});
  });
});
