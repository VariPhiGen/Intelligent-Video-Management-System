/**
 * The device wall's honesty rules, as pure functions.
 *
 * StatusPanel rendered a BOOLEAN from the day it was written — connected, or
 * "Home Assistant bridge offline". That was fine while there was no bridge and
 * false was always right. With a bridge it is wrong: a site that has none
 * configured is not a site whose bridge is broken, and telling an operator
 * "offline" for a deployment that was never meant to have one sends them
 * looking for a fault that does not exist.
 */
import { describe, expect, it } from 'vitest';

import {
  bindingPayload, bridgeLabel, canControl, defaultTopics, stateTone, topicLoops,
  STALE_AFTER_MS,
} from '@/lib/bridge';

const NOW = new Date('2026-09-18T12:00:00Z');
const iso = (msAgo: number) => new Date(NOW.getTime() - msAgo).toISOString();

describe('bridgeLabel', () => {
  it('distinguishes never-configured from broken', () => {
    expect(bridgeLabel('not_configured')).toBe('No bridge configured');
    expect(bridgeLabel('offline')).toBe('Bridge offline — no device state');
    expect(bridgeLabel('connected')).toBe('Bridge connected');
    expect(bridgeLabel('degraded')).toBe('Bridge degraded — some devices unreachable');
  });

  it('never calls an unconfigured bridge offline', () => {
    // The specific wrong answer this function exists to prevent.
    expect(bridgeLabel('not_configured').toLowerCase()).not.toContain('offline');
  });
});

describe('stateTone', () => {
  it('renders a stale reading as unknown, never as current', () => {
    // The whole point of the column. A value from 30 minutes ago shown beside
    // live video reads as "the gate is closed right now", and it is not.
    expect(stateTone({ lastState: 'closed', lastSeen: iso(30 * 60_000) }, NOW))
      .toBe('unknown');
  });

  it('renders a fresh reading', () => {
    expect(stateTone({ lastState: 'closed', lastSeen: iso(30_000) }, NOW))
      .toBe('closed');
  });

  it('renders unknown when nothing has ever reported', () => {
    expect(stateTone({ lastState: null, lastSeen: null }, NOW)).toBe('unknown');
  });

  it('renders unknown when a state has no timestamp to age it', () => {
    // Cannot be shown as current, because nothing says when it was true.
    expect(stateTone({ lastState: 'closed', lastSeen: null }, NOW)).toBe('unknown');
  });

  it('treats the threshold itself as still fresh', () => {
    expect(stateTone({ lastState: 'open', lastSeen: iso(STALE_AFTER_MS) }, NOW))
      .toBe('open');
    expect(stateTone({ lastState: 'open', lastSeen: iso(STALE_AFTER_MS + 1) }, NOW))
      .toBe('unknown');
  });

  it('does not treat a device slightly ahead of us as stale', () => {
    // Clock skew runs both ways.
    const ahead = new Date(NOW.getTime() + 30_000).toISOString();
    expect(stateTone({ lastState: 'closed', lastSeen: ahead }, NOW)).toBe('closed');
  });

  it('renders unknown rather than crashing on an unparseable timestamp', () => {
    expect(stateTone({ lastState: 'closed', lastSeen: 'not-a-date' }, NOW))
      .toBe('unknown');
  });
});

describe('canControl', () => {
  const caps = { peripheral_control: true };
  const device = { enabled: true, bindingVerified: true };

  it('needs the capability, a verified binding and a live bridge', () => {
    expect(canControl(caps, device, 'connected').ok).toBe(true);
  });

  it('refuses without the capability, and says why', () => {
    const r = canControl({ peripheral_control: false }, device, 'connected');
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/permission/i);
  });

  it('refuses while the permission map is still loading', () => {
    // A button offered optimistically turns into a 403 the operator reads as
    // the product being broken.
    expect(canControl(undefined, device, 'connected').ok).toBe(false);
  });

  it('refuses an unverified binding, and says why', () => {
    const r = canControl(caps, { ...device, bindingVerified: false }, 'connected');
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/not been verified/i);
  });

  it('refuses when the bridge is down, and says why', () => {
    expect(canControl(caps, device, 'offline').reason).toMatch(/offline/i);
    expect(canControl(caps, device, 'not_configured').reason).toMatch(/no bridge/i);
  });

  it('allows control on a degraded bridge', () => {
    // Degraded means SOME devices are unreachable. Refusing every one of them
    // would turn one broken driver into a site-wide outage.
    expect(canControl(caps, device, 'degraded').ok).toBe(true);
  });

  it('refuses a disabled device', () => {
    // enabled=false is an operator's explicit "leave this alone", often
    // because someone's hands are near it.
    expect(canControl(caps, { ...device, enabled: false }, 'connected').ok)
      .toBe(false);
  });

  it('reports the device being disabled before the bridge being down', () => {
    // Both are true; the one the operator can act on is the device.
    const r = canControl(caps, { enabled: false, bindingVerified: true }, 'offline');
    expect(r.reason).toMatch(/disabled/i);
  });
});

describe('bindingPayload', () => {
  it('sends null state_topic for a write-only device, not an empty string', () => {
    // A relay that reports nothing back is a real configuration. "" would be a
    // topic nobody publishes to, which is a different and wrong thing.
    const p = bindingPayload({ driver: 'mqtt', address: 'a/set', state_topic: '' });
    expect(p.state_topic).toBeNull();
  });

  it('ignores a state topic for drivers that have none', () => {
    const p = bindingPayload({
      driver: 'homeassistant', address: 'switch.gate', state_topic: 'leftover/topic',
    });
    expect(p.state_topic).toBeNull();
  });

  it('trims whitespace off the address', () => {
    // A trailing space in a topic is invisible in the form and fatal at the
    // broker.
    expect(bindingPayload({ driver: 'mqtt', address: '  a/set  ' }).address).toBe('a/set');
  });

  it('omits the secret entirely when blank, rather than clearing it', () => {
    const p = bindingPayload({ driver: 'mqtt', address: 'a/set', secret: '' });
    expect('secret' in p).toBe(false);
  });

  it('includes the secret when one was typed', () => {
    const p = bindingPayload({ driver: 'mqtt', address: 'a/set', secret: 'tok' });
    expect(p.secret).toBe('tok');
  });
});

describe('topicLoops', () => {
  it('catches an identical state and command topic', () => {
    expect(topicLoops('a/set', 'a/set')).toBe(true);
  });

  it('catches a wildcard that covers the command topic', () => {
    // Subscribing to a/# while publishing to a/set is the same loop wearing a
    // wildcard: the bridge records its own command as device state.
    expect(topicLoops('a/set', 'a/#')).toBe(true);
    expect(topicLoops('vms/siren/x/set', 'vms/siren/+/set')).toBe(true);
  });

  it('allows distinct topics', () => {
    expect(topicLoops('a/set', 'a/state')).toBe(false);
    expect(topicLoops('vms/siren/x/set', 'vms/siren/x/state')).toBe(false);
  });

  it('treats a missing state topic as fine', () => {
    expect(topicLoops('a/set', '')).toBe(false);
    expect(topicLoops('a/set', null)).toBe(false);
  });
});

describe('defaultTopics', () => {
  it('derives both topics from the slug, and they never overlap', () => {
    // The generated pair must not trip the loop check, or the screen would
    // offer a default it then refuses to save.
    const t = defaultTopics('site-siren-tmay');
    expect(t.command).toBe('vms/site-siren-tmay/set');
    expect(t.state).toBe('vms/site-siren-tmay/state');
    expect(topicLoops(t.command, t.state)).toBe(false);
  });

  it('honours a non-default prefix', () => {
    expect(defaultTopics('gate-3', 'site42').command).toBe('site42/gate-3/set');
  });
});
