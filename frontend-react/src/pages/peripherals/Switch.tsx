/** Switch — the peripherals toggle. `tone` colours the ON track: accent for a
 *  normal on/off, green for the "dry run / safe" toggle. */
import type { CSSProperties } from 'react';

export function Switch({ checked, onChange, tone = 'accent', title }: {
  checked: boolean;
  onChange: (v: boolean) => void;
  tone?: 'accent' | 'green';
  title?: string;
}) {
  return (
    <label className="pk-switch" title={title}
           style={{ '--sw-on': tone === 'green' ? 'var(--green)' : 'var(--accent)' } as CSSProperties}>
      <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} />
      <span className="track" />
      <span className="knob" />
    </label>
  );
}
