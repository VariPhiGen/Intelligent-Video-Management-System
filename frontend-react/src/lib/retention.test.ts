/**
 * retention.test.ts — how a retention period is spelled in the browser.
 *
 * The API speaks MINUTES. Every input here is a number plus a unit, so an
 * operator setting a 30-day evidence camera still types "30" and picks "days",
 * while a lobby camera kept for twenty minutes becomes expressible at all.
 *
 * The cases below are the ones that would go wrong silently. A period is a
 * DELETION DEADLINE: rounding it, or letting "0.5 days" through as 720 minutes
 * an operator did not mean, produces footage that disappears earlier or later
 * than the screen said it would, with nothing to notice it by.
 */
import { describe, it, expect } from 'vitest';
import {
  formatRetention, formatApproxRetention, splitRetention, toMinutes,
  retentionError, MINUTES_PER_DAY, MAX_RETENTION_MINUTES,
} from './retention';

describe('formatRetention', () => {
  it('uses the largest whole unit that fits', () => {
    expect(formatRetention(30 * MINUTES_PER_DAY)).toBe('30 days');
    expect(formatRetention(120)).toBe('2 hours');
    expect(formatRetention(45)).toBe('45 minutes');
  });

  it('singularises', () => {
    expect(formatRetention(1)).toBe('1 minute');
    expect(formatRetention(60)).toBe('1 hour');
    expect(formatRetention(MINUTES_PER_DAY)).toBe('1 day');
  });

  it('falls back to minutes rather than inventing a fraction', () => {
    // 90 minutes is an hour and a half; "1.5 hours" reads as a measurement
    // and "2 hours" would be wrong. The exact figure is the honest one.
    expect(formatRetention(90)).toBe('90 minutes');
  });

  it('says "the appliance default" rather than printing a number for null', () => {
    expect(formatRetention(null)).toBe('the appliance default');
  });
});

/**
 * Both cases below were found by driving the real Recording tab in a browser,
 * not by these tests — the unit round-tripped fine while the screen said
 * something an operator would have acted on wrongly.
 */
describe('formatApproxRetention', () => {
  it('spells a MEASURED span in a unit a human reads', () => {
    // The shorten-retention warning measured 43,155 minutes of footage and
    // printed exactly that, because the exact formatter refuses to invent a
    // fraction. "Roughly 43155 minutes ... will be deleted" made the operator
    // do division at the worst possible moment.
    expect(formatApproxRetention(43155)).toBe('~30 days');
  });

  it('keeps one decimal only where it carries information', () => {
    expect(formatApproxRetention(30 * MINUTES_PER_DAY)).toBe('~30 days');
    expect(formatApproxRetention(90)).toBe('~1.5 hours');
    expect(formatApproxRetention(12)).toBe('~12 minutes');
  });

  it('singularises', () => {
    expect(formatApproxRetention(MINUTES_PER_DAY)).toBe('~1 day');
    expect(formatApproxRetention(60)).toBe('~1 hour');
  });

  it('says so when the amount could not be measured', () => {
    expect(formatApproxRetention(null)).toBe('an unknown amount');
  });
});

describe('the confirm-button label', () => {
  it('carries the unit, not a bare number', () => {
    // The button read `Reduce to ${newEffective} days` — a hard-coded unit, so
    // a 45-MINUTE change offered "Reduce to 45 days" on the last screen before
    // footage is permanently deleted. It formats the period now.
    expect(`Reduce to ${formatRetention(45)}`).toBe('Reduce to 45 minutes');
    expect(`Reduce to ${formatRetention(30 * MINUTES_PER_DAY)}`).toBe('Reduce to 30 days');
  });
});

describe('splitRetention', () => {
  it('re-opens a stored period in the unit an operator would have typed', () => {
    // Stored as 43200. Showing that in the box would make an ordinary 30-day
    // camera look like something nobody configured on purpose.
    expect(splitRetention(30 * MINUTES_PER_DAY)).toEqual({ value: '30', unit: 'days' });
    expect(splitRetention(120)).toEqual({ value: '2', unit: 'hours' });
    expect(splitRetention(20)).toEqual({ value: '20', unit: 'minutes' });
  });

  it('leaves the box blank for a camera on the appliance default', () => {
    expect(splitRetention(null)).toEqual({ value: '', unit: 'days' });
  });

  it('round-trips through toMinutes', () => {
    for (const m of [1, 20, 60, 90, 1440, 43200]) {
      const { value, unit } = splitRetention(m);
      expect(toMinutes(value, unit)).toBe(m);
    }
  });
});

describe('toMinutes', () => {
  it('converts each unit', () => {
    expect(toMinutes('20', 'minutes')).toBe(20);
    expect(toMinutes('2', 'hours')).toBe(120);
    expect(toMinutes('30', 'days')).toBe(43200);
  });

  it('treats a blank box as the reset-to-default sentinel', () => {
    // 0 is what the API has always read as "clear the override", and is
    // distinct from omitting the field, which means "leave it alone".
    expect(toMinutes('', 'days')).toBe(0);
  });
});

describe('retentionError', () => {
  it('accepts an ordinary period and a blank box', () => {
    expect(retentionError('30', 'days')).toBeNull();
    expect(retentionError('', 'days')).toBeNull();
    expect(retentionError('20', 'minutes')).toBeNull();
  });

  it('refuses a fraction rather than rounding it', () => {
    // "0.5 days" is a deletion deadline someone would be surprised by later.
    expect(retentionError('0.5', 'minutes')).not.toBeNull();
  });

  it('allows a fraction that lands on a whole minute', () => {
    expect(retentionError('0.5', 'hours')).toBeNull();
    expect(toMinutes('0.5', 'hours')).toBe(30);
  });

  it('holds the same ceiling the API enforces', () => {
    expect(retentionError('3650', 'days')).toBeNull();
    expect(retentionError('3651', 'days')).not.toBeNull();
    expect(retentionError(String(MAX_RETENTION_MINUTES + 1), 'minutes')).not.toBeNull();
  });

  it('refuses text', () => {
    expect(retentionError('soon', 'days')).not.toBeNull();
  });
});
