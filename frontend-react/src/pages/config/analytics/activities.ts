/**
 * activities.ts — activity-type helpers.
 *
 * The catalog is the CPU analytics engine's activity registry, copied into the
 * database by camera-mgmt and served at GET /cameras/analytics/types (loaded by
 * useActivityCatalog). Which activities exist, whether they can run, the zones
 * each takes and every setting's definition come from the engine's code; an
 * administrator only names and colours them.
 *
 * DEFAULT_ACTIVITY_CATALOG is only an offline fallback for labels. It carries no
 * status, so nothing can be added from it.
 */
/** One setting an activity's code declares, rendered as a control in the
 *  activity drawer beneath the built-in schedule. */
export interface ParamField {
  key: string;
  label: string;
  kind: 'number' | 'text' | 'bool' | 'list' | 'json' | 'enum';
  default?: unknown;
  min?: number;
  max?: number;
  unit?: string;
  help?: string;
  /** The choices of an `enum`; the items a `list` may hold. */
  options?: (string | number)[] | null;
  /** A number that must be whole. */
  integer?: boolean;
  /** A list must keep at least this many items. */
  min_items?: number | null;
  /** False = declared by the code but not offered to administrators. */
  configurable?: boolean;
}

/** available = the engine runs it; hold = known, not implemented; unregistered =
 *  the engine no longer lists it (kept for stored configs and old events). */
export type ActivityStatus = 'available' | 'hold' | 'unregistered';
/** optional = no zone means the whole frame; required = needs a zone;
 *  tripwire = lines, never the whole frame. */
export type ZoneRule = 'optional' | 'required' | 'tripwire';

export interface ActivityMeta {
  key: string;
  label: string;
  color: string;
  /** This type's settings; [] means it has none beyond the schedule. */
  params_schema?: ParamField[];
  status?: ActivityStatus;
  zone_rule?: ZoneRule;
  description?: string | null;
  definition_version?: number | null;
}

export const STATUS_LABEL: Record<ActivityStatus, string> = {
  available: 'Available',
  hold: 'On hold',
  unregistered: 'Not in this build',
};

/** Can this activity be added to a camera — does the engine run it? */
export function isAvailable(meta: ActivityMeta | undefined): boolean {
  return meta?.status === 'available';
}

/** Does this activity watch the whole frame when it has no zone? */
export function zoneOptional(meta: ActivityMeta | undefined): boolean {
  return meta?.zone_rule === 'optional';
}

/** The settings an administrator is offered. */
export function configurableFields(meta: ActivityMeta | undefined): ParamField[] {
  return (meta?.params_schema || []).filter(f => f.configurable !== false);
}

export const DEFAULT_ACTIVITY_CATALOG: ActivityMeta[] = [
  { key: 'entry_exit_WLE_logs', label: 'Entry / exit', color: '#4fd1c5' },
  { key: 'stray_parking', label: 'Stray parking', color: '#ffb020' },
  { key: 'people_gathering', label: 'People gathering', color: '#ff8a65' },
  { key: 'no_person_area', label: 'No-person area', color: '#ff8a65' },
  { key: 'restricted_zone_entry', label: 'Restricted zone entry', color: '#4fd1c5' },
  { key: 'car_detection', label: 'Car detection', color: '#ff8a65' },
  { key: 'idle_worker', label: 'Idle worker', color: '#4fd1c5' },
];

/** Palette for zones, cycled as zones are created. */
export const ZONE_COLORS = ['#4fd1c5', '#ffb020', '#ef5350', '#b388ff', '#52c77e', '#ff8a65'];

/** Blend a colour toward the frame's dark ground, to recede the shapes the
 *  operator isn't pointing at. Returns hex because the canvas appends an alpha
 *  suffix to fill colours (`color + '33'`), which rgba() would break. */
export function dimHex(hex: string, t = 0.62): string {
  const m = /^#([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  const toward = [0x1b, 0x22, 0x2b];
  const parts = [(n >> 16) & 255, (n >> 8) & 255, n & 255]
    .map((c, i) => Math.round(c + (toward[i] - c) * t).toString(16).padStart(2, '0'));
  return `#${parts.join('')}`;
}
