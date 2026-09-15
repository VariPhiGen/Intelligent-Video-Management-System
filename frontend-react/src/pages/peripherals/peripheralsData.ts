/**
 * peripheralsData.ts — peripherals data, now split down the middle.
 *
 * REAL (migration 029): the device inventory. `usePeripherals()` reads
 * `/api/peripherals` — rows an operator entered, referenced by the Map tab's
 * pins through a foreign key. Add / edit / delete go back to that API.
 *
 * STILL DEMO: device *state* and everything downstream of it — automation
 * rules, the trigger log, and the builder's dropdown vocabularies. There is no
 * Home-Assistant bridge, so nothing reports whether a light is on and nothing
 * can switch one. `last_state` is deliberately null on every row until an
 * integration writes it, and the UI renders that as "unknown" rather than
 * inventing a status. A rule that cannot fire is not more real for being in
 * Postgres, so rules stay local-state until the engine exists.
 *
 * The hook is still the seam: when the bridge lands it fills `last_state` /
 * `last_seen` and flips `bridgeConnected`, and the components don't change.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { apiFetch } from '@/lib/api';

/** How a device reads at a glance. Drives the icon + status colour so the whole
 *  wall is scannable: green = secure/ready, amber = energised, red = open/fault,
 *  dim = idle. */
export type Tone = 'green' | 'amber' | 'red' | 'dim';

export type DeviceCategory = 'Lighting' | 'Access control' | 'Audio · Sensors';

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
  /** Bridge-reported, null until one exists. */
  lastState?: string | null;
  lastSeen?: string | null;
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
  last_seen?: string | null;
  placements: number;
}

const CATEGORY_GLYPH: Record<DeviceCategory, string> = {
  'Lighting': '☀',
  'Access control': '⊟',
  'Audio · Sensors': '◈',
};

/** Row → what the tiles and map pins render. Everything visual is derived
 *  here, so the only things an operator types are things they can know. */
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
    glyph: CATEGORY_GLYPH[r.category] ?? '◍',
    // No bridge → no state. "UNKNOWN" in dim grey is the honest render; the
    // old code showed a confident ON/LOCKED that nothing had verified.
    state: r.last_state ? r.last_state.toUpperCase() : (r.enabled ? 'UNKNOWN' : 'DISABLED'),
    tone: r.last_state ? 'green' : 'dim',
  };
}

export interface Rule {
  id: string;
  trigger: string;
  action: string;
  enabled: boolean;
  /** Human "when it last changed" label; demo-static. */
  age: string;
}

// TriggerEntry / Outcome removed with the fabricated log — reintroduce them
// alongside the table that actually records fired triggers.


// The device wall is no longer hardcoded — see `usePeripherals()` below. What
// remains demo data on this page is everything that needs a bridge to be true:
// rules, the trigger log, and the builder's vocabularies.

// Category render order — matches the status panel's grouping.
export const CATEGORY_ORDER: DeviceCategory[] = ['Lighting', 'Access control', 'Audio · Sensors'];

// Rules start empty. The three that used to be seeded here ("ANPR hot-list
// match → Barrier down…") described automations nobody had built, on a rule
// engine that does not exist — they read as configuration, not as a mockup.
const RULES: Rule[] = [];

// The trigger log is gone entirely (it lived here as six fabricated rows with
// invented timestamps and an operator name). Nothing fires a peripheral, so
// there is nothing to log; TriggerLog.tsx says exactly that instead.

// Event types, camera/zone targets and HA entities are no longer invented —
// they come from the activity-type catalogue, the camera registry and the
// peripheral inventory respectively. See `eventTypes` on the hook below,
// `zoneTargets()` and `deviceTargets()`.

/** Actions a device of this category could perform, once a bridge can perform
 *  them. Derived from the category rather than a hand-written list, so the
 *  dropdown can never offer "HVAC shutdown" for a door strike. */
export function actionsFor(category: DeviceCategory | null): string[] {
  switch (category) {
    case 'Lighting': return ['Turn on', 'Turn off', 'Flash'];
    case 'Access control': return ['Lock', 'Unlock'];
    case 'Audio · Sensors': return ['Sound siren', 'Play announcement'];
    default: return [];
  }
}

/** How long an action should hold. A vocabulary of time spans, not data —
 *  nothing about it claims a device or event exists. */
export const DURATIONS = ['Until alarm cleared', '30 seconds', '2 minutes', '10 minutes', 'Until manually reset'];

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
  /** False until a Home-Assistant bridge exists. It is not a setting — the
   *  wall reads it to say, truthfully, that no device state is available. */
  bridgeConnected: boolean;
  devices: Device[];
  devicesLoading: boolean;
  devicesError: string;
  deviceStats: { total: number; enabled: number; unplaced: number; categories: number };
  /** The AI activities this VMS can actually detect — the admin-managed
   *  catalogue behind the CMM type dropdown, not a hand-written list. */
  eventTypes: string[];
  /** Real, persisted: these hit /api/peripherals. */
  reloadDevices: () => Promise<void>;
  createDevice: (input: DeviceInput) => Promise<void>;
  updateDevice: (id: string, input: Partial<DeviceInput>) => Promise<void>;
  deleteDevice: (id: string) => Promise<void>;
  rules: Rule[];
  /** Demo-only mutations so the UI reacts; a rule engine replaces these. */
  toggleRule: (id: string) => void;
  removeRule: (id: string) => void;
  addRule: (trigger: string, action: string) => void;
}

/** Devices from the API; rules and the trigger log still local. */
export function usePeripherals(): PeripheralsData {
  const [rules, setRules] = useState<Rule[]>(RULES);
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

  useEffect(() => { void reloadDevices(); }, [reloadDevices]);

  // Trigger vocabulary = what the analytics catalogue says this deployment can
  // detect. Empty on failure rather than falling back to invented types: an
  // empty dropdown is a visible problem, a plausible fake one is not.
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    apiFetch<{ key: string; label: string }[]>('/cameras/analytics/types')
      .then(rows => { if (!cancelled) setEventTypes(rows.map(r => r.label)); })
      .catch(() => { if (!cancelled) setEventTypes([]); });
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

  const toggleRule = useCallback((id: string) => {
    setRules(rs => rs.map(r => (r.id === id ? { ...r, enabled: !r.enabled } : r)));
  }, []);

  const removeRule = useCallback((id: string) => {
    setRules(rs => rs.filter(r => r.id !== id));
  }, []);

  const addRule = useCallback((trigger: string, action: string) => {
    setRules(rs => [{ id: `r-${rs.length}-${trigger.slice(0, 4)}`, trigger, action, enabled: true, age: 'just now' }, ...rs]);
  }, []);

  // Counts that are true without a bridge. "Healthy / faults" left with it:
  // nothing reports health, so those numbers could only have been invented.
  const deviceStats = useMemo(() => ({
    total: devices.length,
    enabled: devices.filter(d => d.enabled).length,
    unplaced: devices.filter(d => d.placements === 0).length,
    categories: new Set(devices.map(d => d.category)).size,
  }), [devices]);

  return {
    // No bridge exists. This was hardcoded `true`, so the wall claimed a
    // connection to something that was never built.
    bridgeConnected: false,
    devices,
    eventTypes,
    devicesLoading,
    devicesError,
    deviceStats,
    reloadDevices,
    createDevice,
    updateDevice,
    deleteDevice,
    rules,
    toggleRule,
    removeRule,
    addRule,
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
