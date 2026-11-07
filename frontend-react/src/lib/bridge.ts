/**
 * bridge.ts — what the device wall is allowed to claim.
 *
 * Three rules, each of which existed only as a comment in StatusPanel.tsx while
 * there was no bridge, and each of which now has to be enforced:
 *
 *   1. "no bridge configured" and "bridge offline" are DIFFERENT FACTS. One is
 *      a supported deployment; the other is a fault worth investigating.
 *   2. A stale reading renders as unknown. A value from half an hour ago shown
 *      beside live video reads as current, and it is not.
 *   3. A control is enabled only when capability, verified binding and a live
 *      bridge all hold — and when it is not, the button says WHICH is missing.
 *      "Disabled, no reason given" is how an operator concludes the product is
 *      broken.
 */
import { apiFetch } from './api';

export type BridgeState = 'connected' | 'degraded' | 'offline' | 'not_configured';

/** Beyond this, a reading is history rather than status. Mirrors the API's
 *  BRIDGE_STATE_STALE_S, which is what actually clears the column. */
export const STALE_AFTER_MS = 5 * 60 * 1000;

export interface BridgeHealth {
  state: BridgeState;
  drivers: Record<string, BridgeState>;
}

export interface Binding {
  driver: 'mqtt' | 'homeassistant' | 'webhook';
  address: string;
  config?: Record<string, any>;
  state_topic?: string | null;
  verified_at?: string | null;
  verify_error?: string | null;
}

export const bridgeHealth = () =>
  apiFetch<BridgeHealth>('/peripherals/bridge/health');

export const putBinding = (slug: string, binding: Binding & { secret?: string }) =>
  apiFetch<Binding>(`/peripherals/${slug}/binding`, {
    method: 'PUT', body: JSON.stringify(binding),
  });

export const deleteBinding = (slug: string) =>
  apiFetch<{ unbound: string }>(`/peripherals/${slug}/binding`, { method: 'DELETE' });

export const sendCommand = (slug: string, command: string,
                            params: Record<string, any> = {}) =>
  apiFetch<{ ok: boolean; detail: string }>(`/peripherals/${slug}/command`, {
    method: 'POST', body: JSON.stringify({ command, params }),
  });

// ── What the wall says ───────────────────────────────────────────────────────

export function bridgeLabel(state: BridgeState): string {
  switch (state) {
    case 'connected': return 'Bridge connected';
    case 'degraded': return 'Bridge degraded — some devices unreachable';
    case 'offline': return 'Bridge offline — no device state';
    // NOT an error, and deliberately does not contain the word "offline". A
    // deployment with no bridge is supported, and calling it offline sends an
    // operator looking for a fault that does not exist.
    case 'not_configured': return 'No bridge configured';
    default: return String(state);
  }
}

/** Green / amber / red / dim for the chip beside the label. */
export function bridgeTone(state: BridgeState): string {
  if (state === 'connected') return 'green';
  if (state === 'degraded') return 'amber';
  if (state === 'offline') return 'red';
  return 'dim';           // not_configured is not a fault, so it is not red
}

export function stateTone(
  device: { lastState?: string | null; lastSeen?: string | null },
  now: Date,
): string {
  if (!device.lastState || !device.lastSeen) return 'unknown';
  const seen = new Date(device.lastSeen).getTime();
  // An unparseable timestamp cannot age a reading, so the reading cannot be
  // shown as current.
  if (Number.isNaN(seen)) return 'unknown';
  // Only LATENESS makes a reading stale — a device whose clock runs slightly
  // ahead has not travelled into the past.
  const age = now.getTime() - seen;
  return age > STALE_AFTER_MS ? 'unknown' : device.lastState;
}

export function canControl(
  caps: Record<string, boolean> | undefined | null,
  device: { enabled: boolean; bindingVerified: boolean },
  bridge: BridgeState,
): { ok: boolean; reason?: string } {
  if (!caps?.peripheral_control)
    return { ok: false, reason: 'You do not have permission to operate devices' };
  // Before the bridge checks: both may be true, and the one the operator can
  // actually act on is the device.
  if (!device.enabled)
    return { ok: false, reason: 'This device is disabled' };
  if (bridge === 'not_configured')
    return { ok: false, reason: 'No bridge is configured, so nothing can be switched' };
  if (bridge === 'offline')
    return { ok: false, reason: 'The bridge is offline' };
  // `degraded` deliberately falls through: it means SOME devices are
  // unreachable, and refusing every one of them would turn one broken driver
  // into a site-wide outage.
  if (!device.bindingVerified)
    return { ok: false, reason: "This device's address has not been verified" };
  return { ok: true };
}


// ── Building a binding from a form ───────────────────────────────────────────

/** Turn the binding form's fields into the PUT body.
 *
 *  Extracted from the modal so the rules below are testable without rendering:
 *  they are small, easy to get subtly wrong, and each one has cost somebody an
 *  afternoon somewhere.
 */
export function bindingPayload(form: {
  driver: string; address: string; state_topic?: string; secret?: string;
  config?: Record<string, any>;
}): Record<string, any> {
  const out: Record<string, any> = {
    driver: form.driver,
    address: form.address.trim(),
    // `health: true` on a device-gateway URL makes the bridge check the device
    // itself on every verify tick (lib/deviceGateway.ts). Always sent, so
    // switching a binding away from the gateway clears it.
    config: form.config ?? {},
    // NULL, not "". A write-only device — a relay that reports nothing back —
    // is a real configuration, and an empty string would be a topic nobody
    // publishes to rather than an absence.
    state_topic: form.driver === 'mqtt' && form.state_topic?.trim()
      ? form.state_topic.trim()
      : null,
  };
  // Blank means UNCHANGED, not "clear it". The secret never round-trips to the
  // page, so a blank field is the normal state of an edit and sending "" would
  // silently wipe a working credential.
  if (form.secret) out.secret = form.secret;
  return out;
}

/** Would this state topic make the bridge hear its own commands?
 *
 *  Mirrors the API's validate_topics. Checked here too so the operator is told
 *  before the round trip — the server remains the authority.
 */
export function topicLoops(address: string, stateTopic?: string | null): boolean {
  const a = (address ?? '').trim();
  const s = (stateTopic ?? '').trim();
  if (!a || !s) return false;
  if (a === s) return true;
  if (s.endsWith('/#') && a.startsWith(s.slice(0, -1))) return true;
  if (s.includes('+')) {
    const sp = s.split('/'), ap = a.split('/');
    if (sp.length === ap.length && sp.every((x, i) => x === '+' || x === ap[i])) return true;
  }
  return false;
}


/** The topics the VMS will use for a device, derived from its slug.
 *
 *  THE OPERATOR SHOULD NOT INVENT THESE. The VMS owns this namespace, so
 *  asking someone to type a topic means asking them to guess a convention,
 *  keep it in sync with the firmware by hand, and get a silent no-op when they
 *  fat-finger it. Generating them inverts that: the screen shows the topics and
 *  the operator pastes them into the device.
 *
 *  Mirrors MQTT_TOPIC_PREFIX, which defaults to "vms".
 */
export function defaultTopics(slug: string, prefix = 'vms') {
  return {
    command: `${prefix}/${slug}/set`,
    state: `${prefix}/${slug}/state`,
  };
}
