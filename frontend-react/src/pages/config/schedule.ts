/**
 * schedule.ts — convert between the days-of-month the UI picks and the
 * `recording_schedule` the API stores.
 *
 * The schedule is day-granular: pick which dates (1–31) record; each selected
 * day records the full 24 h. That maps to the API's `monthly` mode with one
 * rule — the chosen days, 00:00→24:00. Months shorter than 31 days simply skip
 * the dates they don't have (the server checks the real day-of-month).
 */
export interface SchRule { days: number[]; start: string; end: string }
export interface WeeklySchedule { mode: 'weekly' | 'monthly'; rules: SchRule[] }

export type DaySet = Set<number>;   // days 1–31

export const allDays = (): DaySet => new Set(Array.from({ length: 31 }, (_, i) => i + 1));

/** Load a stored schedule into the day set. No monthly schedule → every day
 *  (the natural starting point when you switch to Scheduled). */
export function scheduleToDays(schedule: WeeklySchedule | null | undefined): DaySet {
  if (schedule?.mode === 'monthly' && schedule.rules?.length) {
    const s = new Set<number>();
    for (const r of schedule.rules) for (const d of r.days) if (d >= 1 && d <= 31) s.add(d);
    return s;
  }
  return allDays();
}

/** Day set → a monthly, all-day schedule. Empty set → null (caller blocks it). */
export function daysToSchedule(days: DaySet): WeeklySchedule | null {
  const sorted = [...days].filter(d => d >= 1 && d <= 31).sort((a, b) => a - b);
  if (!sorted.length) return null;
  return { mode: 'monthly', rules: [{ days: sorted, start: '00:00', end: '24:00' }] };
}
