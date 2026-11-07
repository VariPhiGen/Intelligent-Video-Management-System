/**
 * RetentionInput.tsx — a retention period as a number plus a unit.
 *
 * One control, three surfaces (the Recording tab, the Storage tab's bulk
 * apply, the sub-track card), because the alternative is each of them deciding
 * for itself how to spell minutes and an operator seeing three answers to the
 * same question. Blank means "the appliance default" everywhere, which is what
 * the API's 0 sentinel means.
 */
import { RETENTION_UNITS, type RetentionUnit } from '@/lib/retention';

export function RetentionInput({
  value, unit, onValue, onUnit, placeholder, width = 120, disabled, ariaLabel,
}: {
  value: string;
  unit: RetentionUnit;
  onValue: (v: string) => void;
  onUnit: (u: RetentionUnit) => void;
  placeholder?: string;
  width?: number;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  return (
    <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <input
        type="number" min={1} step={1} value={value} disabled={disabled}
        aria-label={ariaLabel}
        onChange={e => onValue(e.target.value)}
        placeholder={placeholder}
        style={{ width }}
      />
      <select
        value={unit} disabled={disabled}
        aria-label={ariaLabel ? `${ariaLabel} unit` : 'Retention unit'}
        onChange={e => onUnit(e.target.value as RetentionUnit)}
        style={{ width: 96 }}
      >
        {RETENTION_UNITS.map(u => (
          <option key={u.id} value={u.id}>{u.label}</option>
        ))}
      </select>
    </span>
  );
}
