/**
 * ScheduleDays.tsx — pick which days of the month record, as a 1–31 calendar.
 * Click a day to toggle, drag to paint a run. Filled = record all day. The set
 * repeats every month; months with fewer days skip the dates they don't have.
 */
import { useEffect, useRef } from 'react';
import { allDays, type DaySet } from './schedule';

const DAYS = Array.from({ length: 31 }, (_, i) => i + 1);

export function ScheduleDays({ days, onChange, disabled }: {
  days: DaySet;
  onChange: (d: DaySet) => void;
  disabled?: boolean;
}) {
  // Drag-paint: the first day decides on/off; every day dragged over follows.
  const paint = useRef<boolean | null>(null);
  useEffect(() => {
    const up = () => { paint.current = null; };
    window.addEventListener('mouseup', up);
    return () => window.removeEventListener('mouseup', up);
  }, []);

  const set = (d: number, on: boolean) => {
    if (days.has(d) === on) return;
    const next = new Set(days);
    on ? next.add(d) : next.delete(d);
    onChange(next);
  };
  const startPaint = (d: number) => {
    if (disabled) return;
    paint.current = !days.has(d);
    set(d, paint.current);
  };
  const dragOver = (d: number) => {
    if (!disabled && paint.current !== null) set(d, paint.current);
  };

  return (
    <div className={`sdays${disabled ? ' disabled' : ''}`}>
      <div className="sdays-toolbar">
        <button className="btn-ghost btn-sm" onClick={() => onChange(allDays())} disabled={disabled}>All days</button>
        <button className="btn-ghost btn-sm" onClick={() => onChange(new Set())} disabled={disabled}>Clear</button>
        <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--muted)' }}>
          {days.size} day{days.size === 1 ? '' : 's'} / month
        </span>
      </div>

      <div className="sdays-grid">
        {DAYS.map(d => (
          <span key={d}
                className={`sday${days.has(d) ? ' on' : ''}`}
                onMouseDown={() => startPaint(d)}
                onMouseEnter={() => dragOver(d)}
                title={`Day ${d} — ${days.has(d) ? 'recording' : 'off'}`}>
            {d}
          </span>
        ))}
      </div>

      <div className="d-hint" style={{ marginTop: 12 }}>
        Filled days record around the clock; empty days don’t record. The pattern repeats every month — a month
        without the 29th–31st just skips those. Changes apply within about a minute.
      </div>
    </div>
  );
}
