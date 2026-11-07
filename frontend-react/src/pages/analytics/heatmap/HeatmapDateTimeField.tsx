/**
 * HeatmapDateTimeField — a day picker that only offers days with stored heat,
 * and a time box. Emits the same `YYYY-MM-DDTHH:mm` string as datetime-local.
 *
 * Why not `<input type="date">`: a native date picker cannot grey out single
 * days, and the point here is that an operator SEES which days have a heatmap
 * before picking one -- heat older than the camera's caps has been deleted.
 * Today is always pickable: its heat may still be in the block analytics is
 * filling, which no stored day list knows about yet.
 */
import { useEffect, useMemo, useRef, useState } from 'react';

const WEEKDAYS = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];

function iso(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** The 6x7 grid of a month, Monday first, padded with the neighbours' days. */
function monthGrid(year: number, month: number): Date[] {
  const first = new Date(year, month, 1);
  const lead = (first.getDay() + 6) % 7;
  return Array.from({ length: 42 }, (_, i) => new Date(year, month, 1 - lead + i));
}

export function HeatmapDateTimeField({ value, onChange, label, available, disabled = false }: {
  /** `YYYY-MM-DDTHH:mm`. */
  value: string;
  onChange: (next: string) => void;
  label: string;
  /** Local days with stored heat. Null while loading: nothing is greyed out yet. */
  available: Set<string> | null;
  disabled?: boolean;
}) {
  const [date = '', time = '00:00'] = (value || '').split('T');
  const [open, setOpen] = useState(false);
  const shown = date ? new Date(`${date}T00:00`) : new Date();
  const [view, setView] = useState({ y: shown.getFullYear(), m: shown.getMonth() });
  const box = useRef<HTMLDivElement>(null);
  const today = iso(new Date());

  useEffect(() => {
    if (!open) return;
    setView({ y: shown.getFullYear(), m: shown.getMonth() });
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', esc);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const days = useMemo(() => monthGrid(view.y, view.m), [view]);
  const pickable = (d: string) => d <= today && (d === today || !available || available.has(d));
  const step = (by: number) => setView(v => {
    const d = new Date(v.y, v.m + by, 1);
    return { y: d.getFullYear(), m: d.getMonth() };
  });
  const title = new Date(view.y, view.m, 1)
    .toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

  return (
    <div className="hm-dt" ref={box}>
      <button type="button" className="hm-dt-date" aria-label={`${label} date`}
              aria-haspopup="dialog" aria-expanded={open} disabled={disabled}
              onClick={() => setOpen(o => !o)}>
        {date ? new Date(`${date}T00:00`).toLocaleDateString() : 'Pick a day'}
      </button>
      <input type="time" aria-label={`${label} time`} value={time} disabled={disabled}
             onChange={e => date && onChange(`${date}T${e.target.value || '00:00'}`)} />
      {open && (
        <div className="hm-cal" role="dialog" aria-label={`${label}: pick a day`}>
          <div className="hm-cal-head">
            <button type="button" aria-label="Previous month" onClick={() => step(-1)}>‹</button>
            <span>{title}</span>
            <button type="button" aria-label="Next month" onClick={() => step(1)}>›</button>
          </div>
          <div className="hm-cal-grid">
            {WEEKDAYS.map(w => <span key={w} className="hm-cal-wd">{w}</span>)}
            {days.map(d => {
              const key = iso(d);
              const ok = pickable(key);
              const cls = ['hm-cal-day',
                d.getMonth() !== view.m ? 'is-other' : '',
                key === date ? 'is-picked' : '',
                key === today ? 'is-today' : '',
                ok ? 'has-heat' : 'no-heat'].filter(Boolean).join(' ');
              return (
                <button type="button" key={key} className={cls} disabled={!ok}
                        aria-label={`${d.toLocaleDateString()}${ok ? '' : ', no heatmap'}`}
                        title={ok ? undefined : 'No heatmap stored for this day'}
                        onClick={() => { onChange(`${key}T${time}`); setOpen(false); }}>
                  {d.getDate()}
                </button>
              );
            })}
          </div>
          <div className="hm-cal-key">
            <span><i className="hm-cal-swatch has-heat" /> heatmap stored</span>
            <span><i className="hm-cal-swatch no-heat" /> none</span>
          </div>
        </div>
      )}
    </div>
  );
}
