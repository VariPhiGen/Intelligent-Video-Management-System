/**
 * ActivityRow.tsx — one CMM activity as a card: the type dropdown carries the
 * row's identity (one activity per type per camera), and the line under it
 * states where it looks and how it's tuned, so a collapsed row is still
 * self-describing. The gear opens the detection settings; the leading spine
 * takes the type's colour.
 *
 * A card owns a SET of zones — a chip per assigned zone with a remove
 * affordance, plus a picker offering the zones it doesn't yet watch.
 *
 * The drawer is schedule (the two built-in keys every activity carries)
 * followed by that type's own detector fields, straight off `params_schema` —
 * there is no generic confidence/sampling/cooldown control anymore; a field a
 * type doesn't declare simply isn't shown.
 *
 * Reporting hover/focus upward is what links the row to the coverage map beside
 * it — the parent lights this row's (first) zone on the frame and recedes the
 * others.
 */
import { useEffect, useState, type CSSProperties } from 'react';
import type { AnalyticsRegion } from '@/lib/types';
import { entryArrow, hasEntryDirection } from '@/lib/tripwire';
import {
  STATUS_LABEL, configurableFields, isAvailable, zoneOptional,
  type ActivityMeta, type ParamField,
} from './activities';
import {
  DAYS, describeParams, clamp, parseListInput, formatListValue,
  type ActParams, type EditActivity,
} from './analyticsConfig';

const GLYPH = { zone: '▱', tripwire: '⟋' } as const;

/** "▱ Zone 1" / "⟋ Tripwire 1 · Entry ↑" — the region as one dropdown option label. */
function regionLabel(r: AnalyticsRegion): string {
  if (r.kind !== 'tripwire') return `${GLYPH.zone} ${r.name}`;
  const way = hasEntryDirection(r.direction)
    ? `Entry ${entryArrow(r.points, r.direction) ?? ''}`.trim()
    : 'no entry direction';
  return `${GLYPH.tripwire} ${r.name} · ${way}`;
}

/** One schema-driven detector field, rendered below the built-in schedule.
 *  Its own component so the JSON textarea can hold uncommitted, possibly
 *  invalid, draft text without derailing the rest of the row's state. */
function SchemaField({ field, value, onChange, onInvalidChange }: {
  field: ParamField;
  value: unknown;
  onChange: (value: unknown) => void;
  onInvalidChange: (invalid: boolean) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);   // json/list-in-progress text, else null
  const [invalid, setInvalid] = useState(false);

  // Report this field's validity upward whenever it changes, AND retract it on
  // unmount (the cleanup fires on every re-run too, which just means an extra
  // no-op call — but on the LAST run, when the drawer collapses or the row
  // goes away, it's the only thing that fires, and it fires as `false`).
  // Without this, closing the drawer on an invalid json field abandons the
  // component mid-edit and the parent's block never lifts.
  useEffect(() => {
    onInvalidChange(invalid);
    return () => onInvalidChange(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invalid]);

  // What the field shows: the camera's stored value, or — for a setting this
  // camera never stored, e.g. one an activity gained after the camera was set
  // up — the definition's default, which is exactly what the engine runs with.
  // Nothing is written until the operator changes it.
  const shown = value ?? field.default;

  // A `number` field's in-progress text, reactively — held as a STRING (never
  // formatted back through `Number(...)`) so React's controlled-input
  // reconciler sees the DOM's current value already matches what it's about
  // to set and skips re-assigning `.value`. Skipping that re-assignment is
  // what matters: an <input type="number"> silently empties itself if JS ever
  // sets its `.value` to text that isn't (yet) a complete float, so echoing
  // `Number("0.25")` → "0.25" back at the user mid-keystroke ("0." → 0 → "0")
  // was exactly what turned typing 0.25 into a saved 1.0 before this fix.
  const numText = field.kind === 'number' ? (draft ?? String(shown ?? 0)) : '';
  const numTrim = numText.trim();
  const numBad = field.kind === 'number' && (numTrim === '' || Number.isNaN(Number(numTrim)));
  useEffect(() => {
    if (field.kind === 'number') setInvalid(numBad);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [numBad, field.kind]);

  const help = field.help || undefined;

  // A list with declared options: what the engine supports, as checkboxes.
  const options = field.kind === 'list' && Array.isArray(field.options) ? field.options : null;
  const picked = options && Array.isArray(shown) ? (shown as (string | number)[]).filter(v => options.includes(v)) : [];
  const tooFew = !!options && field.min_items != null && picked.length < field.min_items;
  useEffect(() => {
    if (options) setInvalid(tooFew);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tooFew]);

  if (options) {
    return (
      <div className="act-row" title={help} style={{ flexWrap: 'wrap' }}>
        <span className="act-label">{field.label}</span>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          {options.map(o => (
            <label key={String(o)} style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12.5 }}>
              <input type="checkbox" style={{ width: 'auto' }} checked={picked.includes(o)}
                     onChange={e => onChange(e.target.checked
                       ? [...picked.filter(v => v !== o), o]
                       : picked.filter(v => v !== o))} />
              {String(o)}
            </label>
          ))}
        </div>
        {tooFew && <span style={{ color: 'var(--red)', fontSize: 11 }}>Select at least {field.min_items}</span>}
      </div>
    );
  }

  if (field.kind === 'enum') {
    const choices = field.options || [];
    return (
      <div className="act-row" title={help}>
        <span className="act-label">{field.label}</span>
        <select value={shown == null ? '' : String(shown)}
                onChange={e => onChange(choices.find(o => String(o) === e.target.value) ?? e.target.value)}>
          {choices.map(o => <option key={String(o)} value={String(o)}>{String(o)}</option>)}
        </select>
      </div>
    );
  }

  if (field.kind === 'bool') {
    return (
      <div className="act-row" title={help}>
        <span className="act-label">{field.label}</span>
        <input type="checkbox" aria-label={field.label} style={{ width: 'auto' }} checked={!!shown}
               onChange={e => onChange(e.target.checked)} />
      </div>
    );
  }

  if (field.kind === 'number') {
    return (
      <div className="act-row" title={help}>
        <span className="act-label">{field.label}</span>
        <input type="number" min={field.min ?? undefined} max={field.max ?? undefined}
               step={field.integer ? 1 : 'any'} value={numText}
               style={{ width: 90, borderColor: numBad ? 'var(--red)' : undefined }}
               onChange={e => setDraft(e.target.value)}
               onBlur={() => {
                 const n = Number(numTrim);
                 if (numTrim !== '' && !Number.isNaN(n)) {
                   setDraft(null);
                   // A whole-number setting is saved whole, as the API requires.
                   onChange(clamp(field.integer ? Math.round(n) : n, field));
                 }
                 // else: keep the raw, unparseable text visible (same treatment
                 // as the json field below) rather than silently discarding it.
               }} />
        {field.unit && <span style={{ color: 'var(--muted)' }}>{field.unit}</span>}
        {numBad && <span style={{ color: 'var(--red)', fontSize: 11 }}>Enter a number</span>}
      </div>
    );
  }

  if (field.kind === 'list') {
    const text = draft ?? formatListValue(shown);
    return (
      <div className="act-row" title={help}>
        <span className="act-label">{field.label}</span>
        <input type="text" value={text} style={{ flex: 1 }}
               onChange={e => {
                 setDraft(e.target.value);
                 onChange(parseListInput(e.target.value));
               }}
               onBlur={() => setDraft(null)} />
      </div>
    );
  }

  if (field.kind === 'json') {
    const text = draft ?? JSON.stringify(shown ?? null, null, 2);
    return (
      <div className="act-row" style={{ alignItems: 'flex-start' }} title={help}>
        <span className="act-label" style={{ paddingTop: 6 }}>{field.label}</span>
        <div style={{ flex: 1 }}>
          <textarea rows={3} value={text} spellCheck={false}
                    style={{ width: '100%', fontFamily: 'var(--mono)', fontSize: 12, resize: 'vertical',
                             borderColor: invalid ? 'var(--red)' : undefined }}
                    onChange={e => { setDraft(e.target.value); }}
                    onBlur={e => {
                      try {
                        const parsed = JSON.parse(e.target.value);
                        setInvalid(false);
                        setDraft(null);
                        onChange(parsed);
                      } catch {
                        setInvalid(true);   // keep the raw, unparseable text visible
                      }
                    }} />
          {invalid && <div style={{ color: 'var(--red)', fontSize: 11, marginTop: 4 }}>Invalid JSON</div>}
        </div>
      </div>
    );
  }

  // text
  return (
    <div className="act-row" title={help}>
      <span className="act-label">{field.label}</span>
      <input type="text" value={shown == null ? '' : String(shown)} style={{ flex: 1 }}
             onChange={e => onChange(e.target.value)} />
    </div>
  );
}

export function ActivityRow({
  act, catalog, byKey, regions, regionOrder, paramsOpen,
  onToggleParams, onSetType, onAddZone, onRemoveZone, onPatchParams, onToggleDay, onRemove, onInvalidChange, onPoint,
}: {
  act: EditActivity;
  /** Selectable types for THIS row's dropdown — the parent has already excluded
   *  types used by other cards, keeping this row's own current type. */
  catalog: ActivityMeta[];
  byKey: Record<string, ActivityMeta>;
  regions: Record<string, AnalyticsRegion>;
  regionOrder: string[];
  paramsOpen: boolean;
  onToggleParams: () => void;
  onSetType: (type: string) => void;
  onAddZone: (rid: string) => void;
  onRemoveZone: (rid: string) => void;
  onPatchParams: (patch: Record<string, any>) => void;
  onToggleDay: (day: string) => void;
  onRemove: () => void;
  /** Whether ANY of this row's schema fields currently holds unparseable text —
   *  rolled up so the parent can block Save while it's true. */
  onInvalidChange: (invalid: boolean) => void;
  /** Region id to light on the coverage map (this row's first zone), or null
   *  when the row is left or has none. */
  onPoint?: (rids: string[] | null) => void;
}) {
  const meta = byKey[act.type];
  const color = meta?.color ?? 'var(--border3)';
  const usable = regionOrder.filter(rid => regions[rid]);
  const available = usable.filter(rid => !act.regions.includes(rid));
  const p = act.params;
  const schema = configurableFields(meta);
  const wholeFrame = zoneOptional(meta) && act.regions.length === 0;

  const point = () => onPoint?.(act.regions.length ? act.regions : null);
  const unpoint = () => onPoint?.(null);

  // Aggregate one invalid flag per schema field key into a single "this row has
  // a bad draft" signal for the parent.
  const [fieldInvalid, setFieldInvalid] = useState<Record<string, boolean>>({});
  useEffect(() => {
    onInvalidChange(Object.values(fieldInvalid).some(Boolean));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fieldInvalid]);

  return (
    <div className={`act${paramsOpen ? ' focus' : ''}`}
         style={{ '--act-color': color } as CSSProperties}
         onMouseEnter={point} onMouseLeave={unpoint} onFocus={point} onBlur={unpoint}>
      <div className="act-top">
        <select className="act-type" value={act.type} title="What this camera detects"
                onChange={e => onSetType(e.target.value)}>
          {/* A stored type that has since left the catalog stays selectable so
              editing another row can't silently rewrite it. */}
          {!meta && <option value={act.type}>{act.type}</option>}
          {catalog.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}
        </select>
        <button className={`cfg-icon${paramsOpen ? ' on' : ''}`} title="Detection settings"
                aria-expanded={paramsOpen} onClick={onToggleParams}>⚙</button>
        <button className="cfg-icon danger" title="Remove this activity" onClick={onRemove}>✕</button>
      </div>

      <div className="act-meta">
        {meta && !isAvailable(meta) && (
          <span className="badge badge-gray" title={meta.description || 'Not run by the analytics engine'}>
            {STATUS_LABEL[meta.status ?? 'unregistered']} · not running
          </span>
        )}
        {wholeFrame && (
          <span className="cov-tag" title="No zone configured — this activity watches the whole frame">
            <i style={{ background: 'var(--dim)' }} />Whole frame
          </span>
        )}
        {usable.length === 0 ? (
          !zoneOptional(meta) && (
            <select className="act-zone unset" disabled value=""
                    title="Draw a zone or tripwire under Zones & Analytics first">
              <option value="">No zones drawn yet</option>
            </select>
          )
        ) : (
          <>
            {act.regions.map(rid => regions[rid] && (
              <span key={rid} className="cov-tag">
                <i style={{ background: regions[rid].color }} />
                {regions[rid].name}
                <button type="button" className="act-zone-rm" title={`Stop watching ${regions[rid].name}`}
                        onClick={() => onRemoveZone(rid)}>×</button>
              </span>
            ))}
            {available.length > 0 && (
              <select className="act-zone unset" value=""
                      title="Add a zone or tripwire for this activity to watch"
                      onChange={e => { if (e.target.value) onAddZone(e.target.value); }}>
                <option value="">{act.regions.length ? '+ Add zone…' : 'Pick a zone…'}</option>
                {available.map(rid => <option key={rid} value={rid}>{regionLabel(regions[rid])}</option>)}
              </select>
            )}
          </>
        )}
        <span className="act-params">{describeParams(p, schema.length)}</span>
      </div>

      {paramsOpen && (
        <div className="act-drawer">
          <div className="act-row" style={{ flexWrap: 'wrap' }}>
            <span className="act-label">Active</span>
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12.5 }}>
              <input type="checkbox" style={{ width: 'auto' }} checked={p.active_hours == null}
                     onChange={e => onPatchParams({ active_hours: e.target.checked ? null : { start: '22:00', end: '06:00' } })} />
              All day
            </label>
            {p.active_hours && (
              <>
                <input type="time" value={p.active_hours.start}
                       onChange={e => onPatchParams({ active_hours: { ...p.active_hours!, start: e.target.value } })}
                       style={{ width: 108 }} />
                <span style={{ color: 'var(--muted)' }}>–</span>
                <input type="time" value={p.active_hours.end}
                       onChange={e => onPatchParams({ active_hours: { ...p.active_hours!, end: e.target.value } })}
                       style={{ width: 108 }} />
              </>
            )}
          </div>
          <div className="act-row">
            <span className="act-label">Days</span>
            <div className="act-days">
              {DAYS.map(d => (
                <button key={d} type="button" title={d} aria-pressed={p.active_days.includes(d)}
                        className={`act-day${p.active_days.includes(d) ? ' on' : ''}`}
                        onClick={() => onToggleDay(d)}>{d[0]}</button>
              ))}
            </div>
          </div>

          {schema.length === 0 ? (
            <div className="d-hint" style={{ marginTop: 0 }}>This detection type has no extra settings.</div>
          ) : schema.map(f => (
            <SchemaField key={f.key} field={f} value={p[f.key]}
              onChange={v => onPatchParams({ [f.key]: v })}
              onInvalidChange={bad => setFieldInvalid(m => (m[f.key] === bad ? m : { ...m, [f.key]: bad }))} />
          ))}
        </div>
      )}
    </div>
  );
}
