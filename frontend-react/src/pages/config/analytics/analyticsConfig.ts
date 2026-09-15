/**
 * analyticsConfig.ts — pure helpers for the CMM config contract, shared by the
 * two tabs that author it: AI Config owns the activities, Zones & Analytics owns
 * the regions. Both persist the whole {regions, activities} object through
 * PUT /cameras/{id}/analytics, so each merges its half over the stored other
 * half here. No React, no fetch — plain functions.
 */
import type { AnalyticsActivity, AnalyticsConfig, AnalyticsRegion, Camera } from '@/lib/types';
import { zoneOptional, type ActivityMeta } from './activities';

export const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** Clamp a number into a schema field's declared [min, max], if either is set.
 *  Shared by every number editor across AI Config (ActivityRow.tsx) and the
 *  admin schema editor (ActivityTypesTab.tsx) so a field's bounds are
 *  enforced identically everywhere it's authored. */
export function clamp(n: number, field: { min?: number | null; max?: number | null }): number {
  let v = n;
  if (field.min != null) v = Math.max(field.min, v);
  if (field.max != null) v = Math.min(field.max, v);
  return v;
}

/** Parse a comma-separated `list`-field draft into elements, promoting the
 *  WHOLE array to numbers when every element parses as a finite number —
 *  so a seeded integer array like a detector's `line_draw_y`
 *  (`[0, 100, 190, …]`) survives a round-trip through the UI instead of
 *  being coerced to strings by a naive `.split(',').map(s => s.trim())`.
 *  A single non-numeric element keeps the WHOLE array as strings (the
 *  pipeline's own configs never mix types within one list, so there's no
 *  in-between case to support). Blank elements are dropped; an all-blank
 *  or empty draft yields `[]`. Shared by ActivityRow.tsx's SchemaField and
 *  ActivityTypesTab.tsx's FieldDefaultInput so a `list` field's default and
 *  its runtime value parse identically. */
export function parseListInput(text: string): (string | number)[] {
  const parts = text.split(',').map(s => s.trim()).filter(Boolean);
  if (parts.length > 0 && parts.every(s => Number.isFinite(Number(s)))) {
    return parts.map(Number);
  }
  return parts;
}

/** Display side of `parseListInput`, kept symmetric on purpose: a numeric
 *  array renders exactly like a string array (`0, 100, 190`), so an operator
 *  can't tell which representation is stored just by looking at the editor. */
export function formatListValue(value: unknown): string {
  return Array.isArray(value) ? value.join(', ') : '';
}

/** The built-in schedule every activity carries regardless of type — the two
 *  keys the DeepStream envelope's `time_window` maps to. Detector parameters
 *  (confidence, sampling, cooldown, and everything else) are per-type now and
 *  come entirely from that type's `params_schema`; nothing generic survives
 *  here. */
export interface ActParams {
  active_hours: { start: string; end: string } | null;  // null = 24/7
  active_days: string[];                                // weekday names; all = every day
}

export const DEFAULT_PARAMS: ActParams = {
  active_hours: null, active_days: [...DAYS],
};

/** An activity while being edited: one entry per catalog type, owning the
 *  zones/tripwires it watches (empty while unassigned, which blocks save). */
export interface EditActivity {
  type: string;
  regions: string[];
  params: ActParams & Record<string, any>;
}

/** Translate the schedule into a plain-language phrase for the summary, plus
 *  how many type-specific settings ride along with it — `describeParams`
 *  itself no longer knows what those settings are (that's the catalog's
 *  schema), only how many there are. */
export function describeParams(p: ActParams, fieldCount: number): string {
  const out: string[] = [];
  out.push(p.active_hours ? `${p.active_hours.start}–${p.active_hours.end}` : '24/7');
  const d = p.active_days ?? [];
  if (d.length === 0) out.push('no active days');
  else if (d.length < 7) {
    const weekdays = d.length === 5 && ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].every(x => d.includes(x));
    const weekend = d.length === 2 && d.includes('Sat') && d.includes('Sun');
    out.push(weekdays ? 'weekdays' : weekend ? 'weekends' : d.join(', '));
  }
  out.push(fieldCount > 0 ? `${fieldCount} setting${fieldCount === 1 ? '' : 's'}` : 'no settings');
  return out.join(' · ');
}

/** First unused `${prefix}${n}`, n starting at 1. */
export function freeId(prefix: string, taken: (id: string) => boolean): string {
  let n = 1;
  while (taken(`${prefix}${n}`)) n++;
  return `${prefix}${n}`;
}

/** Next free display name for a freshly drawn region — the lowest unused
 *  "Zone N" / "Tripwire N". Counting existing regions instead would reissue a
 *  name after a deletion (draw 1+2, delete 1, draw → a second "Zone 2"), and
 *  duplicate names are indistinguishable in the zone dropdown even though the
 *  ids differ. */
export function freeRegionName(regions: Record<string, AnalyticsRegion>, kind: 'zone' | 'tripwire'): string {
  const base = kind === 'tripwire' ? 'Tripwire' : 'Zone';
  const taken = new Set(Object.values(regions).map(r => r.name));
  let n = 1;
  while (taken.has(`${base} ${n}`)) n++;
  return `${base} ${n}`;
}

/** Identity over `ActParams` — pulled out on its own so a type change can carry
 *  the schedule forward while the schema fields re-base to the new type's
 *  defaults. Now that `ActParams` IS just the schedule, this only exists to
 *  name that intent at the call site (and to survive a future schedule field
 *  without every caller needing to know its shape). */
export function schedulePart(p: ActParams): ActParams {
  return { active_hours: p.active_hours, active_days: [...(p.active_days || [])] };
}

/** Fresh params for a (possibly just-picked) type: the common defaults plus
 *  this type's schema-field defaults under their keys. */
export function defaultParams(meta: ActivityMeta | undefined): ActParams & Record<string, any> {
  const extra: Record<string, any> = {};
  for (const f of meta?.params_schema || []) {
    if (f.configurable === false) continue;
    extra[f.key] = Array.isArray(f.default) ? [...f.default] : f.default;
  }
  return { ...DEFAULT_PARAMS, ...extra };
}

/** Deep-copy the camera's stored config into editable state, so edits never
 *  mutate the camera object the page holds. `regionOrder` preserves insertion
 *  order for colour cycling and the regions list. */
export function readConfig(camera: Camera): {
  regions: Record<string, AnalyticsRegion>;
  regionOrder: string[];
  activities: EditActivity[];
} {
  const cfg: AnalyticsConfig = camera.analytics_config || { regions: {}, activities: [] };
  const regions = JSON.parse(JSON.stringify(cfg.regions || {})) as Record<string, AnalyticsRegion>;
  const activities: EditActivity[] = (Array.isArray(cfg.activities) ? cfg.activities : []).map(a => ({
    type: a.type,
    regions: Array.isArray(a.regions) ? [...a.regions] : [],
    params: { ...DEFAULT_PARAMS, ...(a.params || {}) } as ActParams & Record<string, any>,
  }));
  return { regions, regionOrder: Object.keys(regions), activities };
}

/** AI Config's save payload: the edited activities over the STORED regions.
 *  Each activity's zone list is filtered to regions that still exist. An
 *  activity whose zones have ALL disappeared is dropped — never silently
 *  widened to the whole frame. An activity with no zone to begin with is kept
 *  only if its zone rule is `optional` (it then watches the whole frame); the
 *  API refuses one whose rule needs a zone.
 *
 *  A `list` setting with declared options is saved with only those options,
 *  so a value the engine no longer supports (e.g. a DeepStream class name)
 *  cannot block the save.
 *
 *  `params` is whitelisted to the two schedule keys plus the type's CURRENT
 *  declared schema-field keys, so legacy keys that predate schema-driven
 *  fields (or a stale key from a schema revision that has since dropped it)
 *  fall away on the next save instead of accumulating forever.
 *
 *  The whitelist only applies when we have a REAL schema array for the type —
 *  checked with `Array.isArray`, not `meta?.params_schema || []`, because
 *  those two cases must NOT be treated alike:
 *    - a live-fetched type with genuinely no extra settings reports
 *      `params_schema: []` (a real, empty array) — whitelist to schedule-only
 *      is exactly correct here, and is in fact the whole point of this
 *      whitelist for a type like that.
 *    - the offline `DEFAULT_ACTIVITY_CATALOG` fallback (used when
 *      `/analytics/types` fails) never sets `params_schema` at all — so
 *      `byKey['stray_parking']` can resolve to a fallback entry with
 *      `params_schema` UNDEFINED, not empty. Treating `undefined` the same
 *      as `[]` would silently strip its real `required_duration_s` (and anything
 *      else) the moment the catalog fetch fails — worse than leaving stale
 *      keys alone.
 *  Same reasoning covers `byKey[type]` being missing entirely (a stored type
 *  the catalog — live or offline — doesn't know at all): pass verbatim. */
export function buildActivitySave(
  stored: AnalyticsConfig | null | undefined,
  activities: EditActivity[],
  byKey: Record<string, ActivityMeta>,
): AnalyticsConfig {
  const regions = stored?.regions || {};
  const out: AnalyticsActivity[] = activities
    .map(a => {
      const meta = byKey[a.type];
      const schema = meta?.params_schema;
      let params: Record<string, any>;
      if (!Array.isArray(schema)) {
        params = { ...a.params };
      } else {
        const allow = new Set(['active_hours', 'active_days', ...schema.map(f => f.key)]);
        params = {};
        for (const k of allow) if (k in a.params) params[k] = a.params[k];
        for (const f of schema) {
          if (f.kind === 'list' && Array.isArray(f.options) && Array.isArray(params[f.key])) {
            params[f.key] = params[f.key].filter((v: unknown) => f.options!.includes(v as string | number));
          }
        }
      }
      const kept = a.regions.filter(rid => !!regions[rid]);
      const drop = a.regions.length > 0 ? kept.length === 0 : !zoneOptional(meta);
      return drop ? null : { type: a.type, regions: kept, params };
    })
    .filter((a): a is AnalyticsActivity => a !== null);
  return { regions, activities: out };
}

/** Zones & Analytics' save payload: the edited regions over the STORED
 *  activities. Regions with no activity watching them are KEPT (you draw zones
 *  before wiring them up); each stored activity's zone list is filtered to
 *  regions that still exist, and an activity that loses ALL its zones is dropped
 *  rather than widened to the whole frame. An activity stored with no zone
 *  (a whole-frame activity) is kept as it is. */
export function buildRegionSave(
  stored: AnalyticsConfig | null | undefined,
  regions: Record<string, AnalyticsRegion>,
  regionOrder: string[],
): AnalyticsConfig {
  const ordered: Record<string, AnalyticsRegion> = {};
  regionOrder.forEach(rid => { if (regions[rid]) ordered[rid] = regions[rid]; });
  const activities: AnalyticsActivity[] = (stored?.activities || [])
    .map(a => ({ ...a, regions: (a.regions || []).filter(rid => !!ordered[rid]), had: (a.regions || []).length }))
    .filter(a => a.regions.length > 0 || a.had === 0)
    .map(({ had: _had, ...a }) => a);
  return { regions: ordered, activities };
}

/** How many STORED activities watch a region — for the delete confirmation. */
export function watcherCount(stored: AnalyticsConfig | null | undefined, rid: string): number {
  return (stored?.activities || []).filter(a => (a.regions || []).includes(rid)).length;
}
