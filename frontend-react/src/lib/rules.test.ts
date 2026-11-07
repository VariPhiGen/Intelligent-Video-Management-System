/**
 * The three rendering decisions in the rules client that are easy to get
 * subtly, dangerously wrong.
 *
 * All three are about a screen telling an operator what the automation WILL
 * do. A label that overstates a rule's reach, an Arm button offered to
 * somebody who cannot arm, or a banner that conflates "switched off" with
 * "we could not find out" each produce a confident wrong answer, which is
 * worse on this page than on any other in the product.
 */
import { describe, expect, it } from 'vitest';

import { armBannerText, canArm, modeLabel, type ArmState } from '@/lib/rules';

const state = (over: Partial<ArmState> = {}): ArmState => ({
  armed: true, by: null, at: null, reason: null, degraded: false, ...over,
});

describe('modeLabel', () => {
  it('names a log-only rule as not ACTING, never as disabled', () => {
    // "Disabled" would be a lie in the one column an operator scans: a
    // log_only rule fires, matches and raises alerts. It just touches nothing.
    expect(modeLabel('log_only')).toBe('Log only');
    expect(modeLabel('armed')).toBe('Armed');
  });

  it('does not invent a label for a mode it does not know', () => {
    // A mode added server-side must read as itself rather than silently
    // rendering as the safe one — "Log only" next to a rule that is not would
    // be the worst possible default.
    expect(modeLabel('something_new' as any)).toBe('something_new');
  });
});

describe('canArm', () => {
  it('requires the capability', () => {
    expect(canArm({ rule_manage: false }, 'log_only')).toBe(false);
    expect(canArm({ rule_manage: true }, 'log_only')).toBe(true);
  });

  it('is false for a rule that is already armed', () => {
    expect(canArm({ rule_manage: true }, 'armed')).toBe(false);
  });

  it('is false when the permission map has not loaded yet', () => {
    // `me` is null until /me answers. Offering the button optimistically means
    // a 403 the operator reads as the product being broken.
    expect(canArm(undefined, 'log_only')).toBe(false);
    expect(canArm({}, 'log_only')).toBe(false);
  });
});

describe('armBannerText', () => {
  it('shows nothing when armed and healthy', () => {
    expect(armBannerText(state())).toBe('');
  });

  it('names who disarmed the engine and why', () => {
    const text = armBannerText(state({
      armed: false, by: 'alice', reason: 'gate flapping',
      at: '2026-09-18T02:00:00Z',
    }));
    expect(text).toContain('alice');
    expect(text).toContain('gate flapping');
  });

  it('still says alerts are being raised, because they are', () => {
    // Disarm stops actuation, never evidence. An operator who reads the banner
    // as "the system has stopped watching" would go looking for a second
    // failure that is not there.
    expect(armBannerText(state({ armed: false, by: 'alice', reason: 'x' })))
      .toContain('Alerts are still being raised');
  });

  it('survives a disarm with no recorded actor', () => {
    // Pre-existing state, or a disarm written by something other than the API.
    const text = armBannerText(state({ armed: false }));
    expect(text).toContain('disarmed');
    expect(text).not.toContain('null');
    expect(text).not.toContain('undefined');
  });

  it('says so when the state could not be CONFIRMED', () => {
    // degraded means the API could not reach Valkey. "Disarmed by someone" and
    // "we do not know" are different facts calling for different actions, and
    // the banner must not collapse them.
    const text = armBannerText(state({ armed: false, degraded: true }));
    expect(text).toContain('could not be confirmed');
    expect(text).not.toContain('disarmed by');
  });

  it('reports degraded even when the cached state still reads armed', () => {
    // The API returns armed=false on a degraded read today, but the UI must
    // not depend on that: degraded is the stronger fact either way.
    expect(armBannerText(state({ armed: true, degraded: true })))
      .toContain('could not be confirmed');
  });
});
