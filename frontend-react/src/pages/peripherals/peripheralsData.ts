/**
 * peripheralsData.ts — peripherals data, now split down the middle.
 *
 * REAL (migration 029): the device inventory. `usePeripherals()` reads
 * `/api/peripherals` — rows an operator entered, referenced by the Map tab's
 * pins through a foreign key. Add / edit / delete go back to that API.
 *
 * STILL DEMO: device *state* and everything downstream of it — automation
 * the builder's dropdown vocabularies. There is no
 * Home-Assistant bridge, so nothing reports whether a light is on and nothing
 * can switch one. Since P2 a bridge CAN exist: `last_state` / `last_seen`
 * are filled by the elected subscriber (services/bridge), and `bridge` is a
 * three-state health rather than the old boolean — a site with no bridge
 * configured is not a site whose bridge is broken.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { apiFetch } from '@/lib/api';
import { bridgeHealth, stateTone, type Binding, type BridgeState } from '@/lib/bridge';

/** How a device reads at a glance. Drives the icon + status colour so the whole
 *  wall is scannable: green = secure/ready, amber = energised, red = open/fault,
 *  dim = idle. */
export type Tone = 'green' | 'amber' | 'red' | 'dim';

/** The closed list the API and ck_peripherals_category enforce (029, 058). */
export type DeviceCategory =
  | 'Lighting' | 'Access control' | 'Audio · Sensors'
  | 'Automation' | 'Climate · Utilities';

export interface Device {
  /** The peripheral's slug — the stable key map placements reference. */
  id: string;
  name: string;
  location: string;
  category: DeviceCategory;
  vendor?: string;
  /** Where a bridge will find it: HA entity id, MQTT topic, relay address. */
  externalId?: string;
  notes?: string;
  enabled: boolean;
  /** How many site plans this device is pinned to. */
  placements: number;
  /** Bridge-reported. The API is the ONLY writer of these two (see
   *  services/bridge), which is what keeps the wall honest by construction. */
  lastState?: string | null;
  lastSeen?: string | null;
  /** Whether the bridge has CONFIRMED this device's address resolves. An
   *  unverified binding refuses to actuate, so the control says why. */
  bindingVerified: boolean;
  /** Where the BRIDGE reaches this device. Null until someone binds it — which
   *  is the normal state of a freshly added peripheral, not a fault. */
  binding: Binding | null;
  // ── derived, not stored ────────────────────────────────────────────────
  /** Category icon — derived, so an operator never has to pick a glyph. */
  glyph: string;
  /** Short uppercase status shown top-right of the tile. `UNKNOWN` whenever
   *  no bridge has reported, which today is always. */
  state: string;
  tone: Tone;
  /** Set when the device is unreachable — a bridge's to report. */
  fault?: string;
}

/** How often the wall re-reads devices (state, health). */
export const DEVICE_POLL_MS = 5_000;

/** API shape of `GET /peripherals`. */
interface PeripheralRow {
  id: string;
  name: string;
  category: DeviceCategory;
  location?: string | null;
  vendor?: string | null;
  external_id?: string | null;
  notes?: string | null;
  enabled: boolean;
  last_state?: string | null;
  binding_verified?: boolean;
  binding?: Binding | null;
  last_seen?: string | null;
  placements: number;
}

const CATEGORY_GLYPH: Record<DeviceCategory, string> = {
  'Lighting': '☀',
  'Access control': '⊟',
  'Audio · Sensors': '◈',
  'Automation': '▶',
  'Climate · Utilities': '❄',
};

/** Row → what the tiles and map pins render. Everything visual is derived
 *  here, so the only things an operator types are things they can know. */
/** The uppercase status on the tile, aged out by stateTone. */
function _display(r: PeripheralRow): string {
  if (!r.enabled) return 'DISABLED';
  const tone = stateTone({ lastState: r.last_state, lastSeen: r.last_seen },
                          new Date());
  return tone === 'unknown' ? 'UNKNOWN' : tone.toUpperCase();
}

/** Dim whenever the tile says UNKNOWN, so a retired reading looks retired. */
function _tone(r: PeripheralRow): Tone {
  if (!r.enabled) return 'dim';
  const tone = stateTone({ lastState: r.last_state, lastSeen: r.last_seen },
                          new Date());
  return tone === 'unknown' ? 'dim' : 'green';
}


function toDevice(r: PeripheralRow): Device {
  return {
    id: r.id,
    name: r.name,
    location: r.location || '—',
    category: r.category,
    vendor: r.vendor || undefined,
    externalId: r.external_id || undefined,
    notes: r.notes || undefined,
    enabled: r.enabled,
    placements: r.placements ?? 0,
    lastState: r.last_state ?? null,
    lastSeen: r.last_seen ?? null,
    bindingVerified: !!r.binding_verified,
    binding: r.binding ?? null,
    glyph: CATEGORY_GLYPH[r.category] ?? '◍',
    // A reading AGES. Showing "closed" for a device that stopped reporting
    // half an hour ago beside live video reads as "right now", and it is not —
    // so stateTone retires it to unknown and the tile goes dim.
    state: _display(r),
    tone: _tone(r),
  };
}

// TriggerEntry / Outcome removed with the fabricated log — reintroduce them
// alongside the table that actually records fired triggers.


// The device wall is no longer hardcoded — see `usePeripherals()` below. What
// remains demo data on this page is everything that needs a bridge to be true:
// the builder's vocabularies.

// Category render order — matches the status panel's grouping.
export const CATEGORY_ORDER: DeviceCategory[] = [
  'Lighting', 'Access control', 'Audio · Sensors', 'Automation', 'Climate · Utilities',
];

// Rules are no longer local state. They are rows in `alert_rules`, created and
// read through /api/rules — see src/lib/rules.ts and RuleBuilder.tsx. What used
// to live here was a browser-session list that never fired, which read as
// configuration and was not.

// The trigger log is gone entirely (it lived here as six fabricated rows with
// invented timestamps and an operator name). Nothing fires a peripheral, so
// there is nothing to log; TriggerLog.tsx says exactly that instead.

// Event types, camera targets and HA entities are no longer invented — they
// come from the activity-type catalogue, the camera registry and the
// peripheral inventory respectively. See `activityTypes` on the hook below.

/** A dropdown choice and the command actually sent to the bridge, which must
 *  stay decoupled: `command` is the literal payload every driver's vocabulary
 *  is built from — HaDriver's per-domain service map, the siren's Modbus
 *  translator (`parse_command`), the plain-relay MQTT default — and every one
 *  of them speaks lowercase `on` / `off` / `lock` / `unlock`, never English.
 *  A sentence like "Sound siren" sent as the command is silently ignored by
 *  all three: confirmed 2026-09-23 when an armed rule published successfully
 *  but the relay never moved. `label` is free to stay human-readable because
 *  nothing downstream ever sees it. */
export interface DeviceAction { label: string; command: string; }

/** Actions a device of this category could perform, once a bridge can perform
 *  them. Derived from the category rather than a hand-written list, so the
 *  dropdown can never offer "HVAC shutdown" for a door strike. */
export function actionsFor(category: DeviceCategory | null): DeviceAction[] {
  switch (category) {
    case 'Lighting': return [
      { label: 'Turn on', command: 'on' },
      { label: 'Turn off', command: 'off' },
      { label: 'Flash', command: 'flash' },
    ];
    // Open/Close for gates and barriers, Unlock/Lock for maglocks and
    // strikes. On a relay channel both pairs mean ON/OFF (the device gateway
    // maps them), so pick the words that match what is wired.
    case 'Access control': return [
      { label: 'Open', command: 'open' },
      { label: 'Close', command: 'close' },
      { label: 'Unlock', command: 'unlock' },
      { label: 'Lock', command: 'lock' },
    ];
    case 'Audio · Sensors': return [
      { label: 'Sound siren', command: 'on' },
      { label: 'Play announcement', command: 'announce' },
    ];
    case 'Automation': return [{ label: 'Run', command: 'run' }];
    case 'Climate · Utilities': return [
      { label: 'Turn on', command: 'on' },
      { label: 'Turn off', command: 'off' },
    ];
    default: return [];
  }
}

/** A Home Assistant device's actions come from WHAT IT IS in HA, not from the
 *  category an operator filed it under: a lock offered "Flash" because it sits
 *  under Lighting is a rule that refuses on every fire. Each list is exactly
 *  what HaDriver's _SERVICES maps for that domain.
 *
 *  `switch` is absent on purpose. A switch is usually a relay channel, which
 *  does whatever is wired to it, and the driver accepts the whole relay
 *  vocabulary for it — so its category's words (Open/Close for a gate,
 *  Unlock/Lock for a maglock) are the right ones. */
const HA_ACTIONS: Record<string, DeviceAction[]> = {
  light: [{ label: 'Turn on', command: 'on' }, { label: 'Turn off', command: 'off' }],
  lock: [{ label: 'Unlock', command: 'unlock' }, { label: 'Lock', command: 'lock' }],
  cover: [{ label: 'Open', command: 'open' }, { label: 'Close', command: 'close' },
          { label: 'Stop', command: 'stop' }],
  siren: [{ label: 'Sound siren', command: 'on' }, { label: 'Silence', command: 'off' }],
  media_player: [{ label: 'Play announcement', command: 'announce' },
                 { label: 'Stop', command: 'stop' },
                 { label: 'Turn on', command: 'on' }, { label: 'Turn off', command: 'off' }],
  scene: [{ label: 'Run', command: 'run' }],
  script: [{ label: 'Run', command: 'run' }, { label: 'Stop', command: 'stop' }],
  button: [{ label: 'Press', command: 'press' }],
  fan: [{ label: 'Turn on', command: 'on' }, { label: 'Turn off', command: 'off' }],
  valve: [{ label: 'Open', command: 'open' }, { label: 'Close', command: 'close' }],
};

/** The HA domain a device is bound to (`lock` for `lock.side_door`), or null
 *  for any other driver. */
export function haDomain(d: Pick<Device, 'binding'> | null): string | null {
  if (!d?.binding || d.binding.driver !== 'homeassistant') return null;
  const addr = d.binding.address || '';
  return addr.includes('.') ? addr.split('.', 1)[0] : null;
}

/** The actions THIS device can perform: by HA domain when it is bound to Home
 *  Assistant, by category otherwise. */
export function actionsForDevice(d: Device | null): DeviceAction[] {
  if (!d) return [];
  const domain = haDomain(d);
  return (domain && HA_ACTIONS[domain]) || actionsFor(d.category);
}

/** Does this action need something to play? (a Home Assistant speaker's
 *  `announce` — the device gateway's speaker plays its own configured sound) */
export function needsMedia(d: Device | null, command: string): boolean {
  return command === 'announce' && haDomain(d) === 'media_player';
}

/** The reverse of `actionsFor`'s `command`, for displaying a rule saved
 *  before this split (or any command a driver echoes back) as English rather
 *  than a bare token. Falls through to the raw value for anything unknown —
 *  never hides what was actually sent. */
const COMMAND_LABEL: Record<string, string> = {
  on: 'Turn on', off: 'Turn off', flash: 'Flash',
  lock: 'Lock', unlock: 'Unlock', announce: 'Play announcement',
  open: 'Open', close: 'Close', stop: 'Stop', run: 'Run', press: 'Press',
  play: 'Play', pause: 'Pause',
};
export function labelForCommand(command: string): string {
  return COMMAND_LABEL[command] ?? command;
}

// DURATIONS moved into RuleBuilder as HOLDS, in seconds, because the API
// stores a number and refuses a hold with no revert command. A vocabulary of
// English time spans could not express that constraint.

export interface DeviceInput {
  name: string;
  category: DeviceCategory;
  location?: string;
  vendor?: string;
  external_id?: string;
  notes?: string;
  enabled?: boolean;
}

export interface PeripheralsData {
  /** Three states, not a boolean. `not_configured` is a supported deployment
   *  and a DIFFERENT FACT from `offline`; the old boolean could not tell them
   *  apart and would have sent an operator hunting a broker that was never
   *  meant to exist. */
  bridge: BridgeState;
  devices: Device[];
  devicesLoading: boolean;
  devicesError: string;
  deviceStats: { total: number; enabled: number; unplaced: number; categories: number };
  /** The AI activities this VMS can actually detect — the admin-managed
   *  catalogue behind the CMM type dropdown, not a hand-written list. Keyed
   *  by the catalog key (what an event's `activity` field actually carries),
   *  not the label, so a rule can be filtered to one camera's configured
   *  activities and still store a value ingest will ever match. */
  activityTypes: { key: string; label: string }[];
  /** Real, persisted: these hit /api/peripherals. */
  reloadDevices: () => Promise<void>;
  createDevice: (input: DeviceInput) => Promise<void>;
  updateDevice: (id: string, input: Partial<DeviceInput>) => Promise<void>;
  deleteDevice: (id: string) => Promise<void>;
}

/** The device inventory, from the API. Rules and alerts have their own
 *  client now (src/lib/rules.ts) and do not pass through here. */
export function usePeripherals(): PeripheralsData {
  const [bridge, setBridge] = useState<BridgeState>('not_configured');
  const [devices, setDevices] = useState<Device[]>([]);
  const [devicesLoading, setDevicesLoading] = useState(true);
  const [devicesError, setDevicesError] = useState('');

  const reloadDevices = useCallback(async () => {
    try {
      const rows = await apiFetch<PeripheralRow[]>('/peripherals');
      setDevices(rows.map(toDevice));
      setDevicesError('');
    } catch (e: any) {
      // Keep whatever is on screen and say why it may be stale, rather than
      // blanking the wall on a transient failure.
      setDevicesError(e?.message || 'Could not load peripherals');
    } finally {
      setDevicesLoading(false);
    }
  }, []);

  // Device state and health change on their own (the bridge re-checks every
  // BRIDGE_VERIFY_INTERVAL_S), so the wall polls rather than loading once.
  // Paused while the tab is hidden: nobody is looking, and a wall left open
  // on a control-room screen overnight should not poll for nobody.
  useEffect(() => {
    void reloadDevices();
    const t = setInterval(() => {
      if (document.visibilityState !== 'hidden') void reloadDevices();
    }, DEVICE_POLL_MS);
    return () => clearInterval(t);
  }, [reloadDevices]);

  // Bridge health, polled: a broker that drops has to stop the wall claiming
  // device state within a minute, not at the next page load. A FAILED read
  // leaves the last known value rather than asserting `offline` — the request
  // failing is not evidence about the broker.
  useEffect(() => {
    let cancelled = false;
    const load = () => bridgeHealth()
      .then(h => { if (!cancelled) setBridge(h.state); })
      .catch(() => { /* keep the last known state; see above */ });
    void load();
    const t = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  // Trigger vocabulary = what the analytics catalogue says this deployment can
  // detect. Empty on failure rather than falling back to invented types: an
  // empty dropdown is a visible problem, a plausible fake one is not.
  const [activityTypes, setActivityTypes] = useState<{ key: string; label: string }[]>([]);
  useEffect(() => {
    let cancelled = false;
    apiFetch<{ key: string; label: string }[]>('/cameras/analytics/types')
      .then(rows => { if (!cancelled) setActivityTypes(rows.map(r => ({ key: r.key, label: r.label }))); })
      .catch(() => { if (!cancelled) setActivityTypes([]); });
    return () => { cancelled = true; };
  }, []);

  const createDevice = useCallback(async (input: DeviceInput) => {
    await apiFetch('/peripherals', { method: 'POST', body: JSON.stringify(input) });
    await reloadDevices();
  }, [reloadDevices]);

  const updateDevice = useCallback(async (id: string, input: Partial<DeviceInput>) => {
    await apiFetch(`/peripherals/${id}`, { method: 'PATCH', body: JSON.stringify(input) });
    await reloadDevices();
  }, [reloadDevices]);

  const deleteDevice = useCallback(async (id: string) => {
    await apiFetch(`/peripherals/${id}`, { method: 'DELETE' });
    await reloadDevices();
  }, [reloadDevices]);

  // Counts that are true without a bridge. "Healthy / faults" left with it:
  // nothing reports health, so those numbers could only have been invented.
  const deviceStats = useMemo(() => ({
    total: devices.length,
    enabled: devices.filter(d => d.enabled).length,
    unplaced: devices.filter(d => d.placements === 0).length,
    categories: new Set(devices.map(d => d.category)).size,
  }), [devices]);

  return {
    bridge,
    devices,
    activityTypes,
    devicesLoading,
    devicesError,
    deviceStats,
    reloadDevices,
    createDevice,
    updateDevice,
    deleteDevice,
  };
}

/** Tone → CSS colour token, shared by the tile icon and its status label. */
export function toneColor(tone: Tone): string {
  switch (tone) {
    case 'green': return 'var(--green)';
    case 'amber': return 'var(--yellow)';
    case 'red': return 'var(--red)';
    default: return 'var(--dim)';
  }
}
