/**
 * DateTimeField — a date box and a time box that together produce one
 * `YYYY-MM-DDTHH:mm` value, the same string `<input type="datetime-local">`
 * emits.
 *
 * Why not just use datetime-local: its picker button in Chromium opens a
 * calendar only. The time is editable, but solely by typing or arrowing the
 * hh:mm segments — there is no clock in the popup — so an operator who reaches
 * for the picker icon can set the day and nothing else, and a clip window ends
 * up at 00:00. Two native inputs each get their own real picker (a calendar and
 * a time spinner), which is what people expect when a field asks for a moment
 * in time.
 *
 * Emits '' when the date is cleared — callers treat that as "unset", so a
 * half-filled field never becomes a silently-wrong timestamp. Picking a time
 * with no date fills today's date, since a bare time cannot be stored.
 */
import type { CSSProperties } from 'react';

import { todayLocal } from '@/lib/format';

export function DateTimeField({
  value, onChange, disabled = false, label, step = 60, min, max,
  style, inputStyle,
}: {
  /** `YYYY-MM-DDTHH:mm` (or '' when unset). */
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
  /** Screen-reader label; each box gets it with "date"/"time" appended. */
  label?: string;
  /** Seconds granularity for the time box. 60 = minutes (default). */
  step?: number;
  /** Bounds, as full `YYYY-MM-DDTHH:mm` values; only the date half is applied. */
  min?: string;
  max?: string;
  /** Wrapper overrides — e.g. a fixed width in a toolbar. */
  style?: CSSProperties;
  /** Applied to BOTH boxes, for panels that style their inputs themselves. */
  inputStyle?: CSSProperties;
}) {
  const [date = '', time = ''] = (value || '').split('T');

  const emit = (nextDate: string, nextTime: string) => {
    // Clearing the date clears the field: a time on its own is not a moment,
    // and keeping a stale date behind an empty box is how you export the
    // wrong day.
    if (!nextDate) { onChange(''); return; }
    onChange(`${nextDate}T${nextTime || '00:00'}`);
  };

  return (
    <div style={{ display: 'flex', gap: 4, minWidth: 0, ...style }}>
      <input
        type="date"
        aria-label={label ? `${label} date` : undefined}
        value={date}
        disabled={disabled}
        min={min ? min.split('T')[0] : undefined}
        max={max ? max.split('T')[0] : undefined}
        onChange={e => emit(e.target.value, time)}
        style={{ flex: '1 1 55%', minWidth: 0, ...inputStyle }}
      />
      <input
        type="time"
        aria-label={label ? `${label} time` : undefined}
        value={time}
        step={step}
        disabled={disabled}
        // A time with no date yet: anchor it to today rather than dropping the
        // input on the floor, so typing left-to-right through the row works.
        onChange={e => emit(date || todayLocal(), e.target.value)}
        style={{ flex: '1 1 45%', minWidth: 0, ...inputStyle }}
      />
    </div>
  );
}
