/** A headline figure. `tint` paints the left rule — reserved for the one stat
 *  that can be in a bad state, so colour still means something on this row. */
export function Stat({ label, value, unit, sub, tint, text, onClick }: {
  label: string; value: string; unit?: string; sub?: string; tint?: string;
  /** The value is a name, not a figure — sized to read as one, and allowed to
   *  wrap. Without it a long identifier is set at numeral size and overflows. */
  text?: boolean;
  onClick?: () => void;
}) {
  return (
    <div className={`stat${onClick ? ' clickable' : ''}`}
         data-tint={tint ? '' : undefined}
         style={tint ? { '--tint': tint } as never : undefined}
         onClick={onClick}>
      <div className={`stat-val${text ? ' stat-val-text' : ''}`}>{value}{unit && <small>{unit}</small>}</div>
      <div className="stat-label">{label}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
