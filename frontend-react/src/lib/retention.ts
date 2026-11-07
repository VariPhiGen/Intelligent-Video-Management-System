/**
 * retention.ts — one place that knows how a retention period is spelled.
 *
 * The API speaks MINUTES. It used to speak whole days, which made a whole class
 * of policy inexpressible: a lobby camera kept for twenty minutes, a test rig
 * kept for an hour, a shift-length window that a DPDP data-minimisation posture
 * actually wants. The recorder writes 60-second segments and deletes whole
 * ones, so a minute is the finest period it can honour — and now the finest an
 * operator can ask for.
 *
 * Operators still think in days for a 30-day camera and in minutes for a
 * 20-minute one, so every input is a NUMBER plus a UNIT, and this module does
 * the conversion in one direction only: to minutes, on the way out. Nothing in
 * the UI stores a period in anything but minutes.
 */

export const MINUTES_PER_HOUR = 60;
export const MINUTES_PER_DAY = 1440;

export type RetentionUnit = 'minutes' | 'hours' | 'days';

export const RETENTION_UNITS: { id: RetentionUnit; label: string; minutes: number }[] = [
  { id: 'minutes', label: 'minutes', minutes: 1 },
  { id: 'hours', label: 'hours', minutes: MINUTES_PER_HOUR },
  { id: 'days', label: 'days', minutes: MINUTES_PER_DAY },
];

/** 3650 days — the ceiling the day-based API always enforced, unchanged. */
export const MAX_RETENTION_MINUTES = 3650 * MINUTES_PER_DAY;

/**
 * Human spelling of a period: "45 minutes", "6 hours", "30 days".
 *
 * Mirrors the server's `format_retention` (camera-mgmt
 * services/retention_units.py). The server sends `retention_label` for the
 * values it owns; this exists for the ones the browser computes itself, such
 * as the preview of an unsaved edit.
 */
export function formatRetention(minutes: number | null | undefined): string {
  if (minutes == null) return 'the appliance default';
  const m = Math.round(minutes);
  if (m % MINUTES_PER_DAY === 0) {
    const n = m / MINUTES_PER_DAY;
    return `${n} ${n === 1 ? 'day' : 'days'}`;
  }
  if (m % MINUTES_PER_HOUR === 0) {
    const n = m / MINUTES_PER_HOUR;
    return `${n} ${n === 1 ? 'hour' : 'hours'}`;
  }
  return `${m} ${m === 1 ? 'minute' : 'minutes'}`;
}

/**
 * A MEASURED span, approximately: "~30 days", "~1.5 hours", "~12 minutes".
 *
 * Distinct from `formatRetention`, which spells an EXACT policy and falls back
 * to raw minutes rather than invent a fraction. That is right for a number
 * somebody typed and wrong for one the system measured: 43,155 minutes of
 * footage is thirty days, and printing "43155 minutes" in a deletion warning
 * makes the operator do the arithmetic at the worst possible moment.
 *
 * One decimal where it carries information and none where it does not, so a
 * whole thirty days reads "~30 days" rather than "~30.0 days". Mirrors
 * `_trim` in services/nvr/storage/alerts.py, which does the same job for the
 * storage-pressure messages.
 */
export function formatApproxRetention(minutes: number | null | undefined): string {
  if (minutes == null) return 'an unknown amount';
  const pick = (v: number, unit: string) => {
    const r = Math.round(v * 10) / 10;
    const body = Number.isInteger(r) ? String(r) : r.toFixed(1);
    return `~${body} ${unit}${body === '1' ? '' : 's'}`;
  };
  if (minutes >= MINUTES_PER_DAY) return pick(minutes / MINUTES_PER_DAY, 'day');
  if (minutes >= MINUTES_PER_HOUR) return pick(minutes / MINUTES_PER_HOUR, 'hour');
  return pick(minutes, 'minute');
}

/**
 * Split minutes into the LARGEST unit that divides it evenly, so a camera
 * stored as 43200 comes back as "30 days" rather than "43200 minutes" and an
 * operator editing it sees the number they typed.
 */
export function splitRetention(
  minutes: number | null | undefined,
): { value: string; unit: RetentionUnit } {
  if (minutes == null) return { value: '', unit: 'days' };
  const m = Math.round(minutes);
  if (m % MINUTES_PER_DAY === 0) return { value: String(m / MINUTES_PER_DAY), unit: 'days' };
  if (m % MINUTES_PER_HOUR === 0) return { value: String(m / MINUTES_PER_HOUR), unit: 'hours' };
  return { value: String(m), unit: 'minutes' };
}

/** A typed value + unit as minutes. NaN propagates so callers can validate. */
export function toMinutes(value: string, unit: RetentionUnit): number {
  const n = value.trim() === '' ? 0 : Number(value);
  if (!Number.isFinite(n)) return NaN;
  const per = RETENTION_UNITS.find(u => u.id === unit)?.minutes ?? MINUTES_PER_DAY;
  return n * per;
}

/**
 * Whether a value+unit pair is something the API will accept: a whole number
 * of minutes, zero (meaning "reset to the appliance default") or up to the
 * ceiling. Fractional units are rejected here rather than silently rounded —
 * "0.5 days" is a deletion deadline someone would then be surprised by.
 */
export function retentionError(value: string, unit: RetentionUnit): string | null {
  const minutes = toMinutes(value, unit);
  if (!Number.isFinite(minutes)) return 'Retention must be a number.';
  if (!Number.isInteger(minutes)) {
    return 'Retention must work out to a whole number of minutes.';
  }
  if (minutes < 0) return 'Retention cannot be negative.';
  if (minutes > MAX_RETENTION_MINUTES) {
    return `Retention cannot exceed ${MAX_RETENTION_MINUTES / MINUTES_PER_DAY} days.`;
  }
  return null;
}
