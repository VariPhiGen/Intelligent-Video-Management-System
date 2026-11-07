import { describe, expect, it } from 'vitest';

import { addDeviceUrl, browserUrl, connectionLabel, importResultLabel } from './homeassistant';

describe('browserUrl', () => {
  it('swaps a loopback HA for the host this page came from', () => {
    // The VMS reaches HA on 127.0.0.1; in a link that is the operator's laptop.
    expect(browserUrl('http://127.0.0.1:8123', '192.168.0.161')).toBe('http://192.168.0.161:8123');
    expect(browserUrl('http://localhost:8123/', 'vms.site')).toBe('http://vms.site:8123');
  });
  it('leaves a reachable host alone', () => {
    expect(browserUrl('https://ha.site:8443', '10.0.0.5')).toBe('https://ha.site:8443');
  });
  it('is null for nothing or nonsense', () => {
    expect(browserUrl(null, 'x')).toBeNull();
    expect(browserUrl('not a url', 'x')).toBeNull();
  });
  it('points "add a device" at HA\'s integrations page', () => {
    expect(addDeviceUrl('http://h:8123')).toBe('http://h:8123/config/integrations/dashboard');
  });
});

describe('connectionLabel', () => {
  it('says which thing to fix', () => {
    expect(connectionLabel({ state: 'offline', error: 'unauthorized' })).toMatch(/token/i);
    expect(connectionLabel({ state: 'offline', error: 'unreachable' })).toMatch(/not reachable/);
  });
  it('not configured is not called offline', () => {
    expect(connectionLabel({ state: 'not_configured', error: null })).not.toMatch(/offline/i);
  });
});

describe('importResultLabel', () => {
  it('explains each refusal', () => {
    expect(importResultLabel({ entity_id: 'a', result: 'name_taken', name: 'Gate' }))
      .toMatch(/Gate/);
    expect(importResultLabel({ entity_id: 'a', result: 'already_bound', peripheral: 'gate-x1' }))
      .toMatch(/gate-x1/);
  });
});
