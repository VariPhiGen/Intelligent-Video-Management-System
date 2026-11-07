/**
 * EventSubscriptionModal — "send these events, from these cameras, to that
 * destination", built as that sentence, top to bottom.
 *
 * Each event group shows only the filters that apply to it (routing.py):
 * activities and motion narrow AI detections, rules and severity narrow
 * alerts, cameras narrow everything. A filter for a group that is not ticked
 * is cleared on save, so nothing invisible is ever stored.
 *
 * An empty list means "all" to the backend, so "Only these" with nothing
 * ticked is refused here rather than silently widening to everything.
 */
import { useState, type ReactNode } from 'react';
import { Modal } from '@/components/Modal';
import type { Destination, Options, Subscription } from '@/lib/eventIntegrations';

type Draft = Omit<Subscription, 'id'>;
type Pick = 'cameras' | 'activities' | 'rules';
const AI = ['ai.event.created', 'ai.event.closed'];
const ALERT = 'alert.raised';
const HEALTH = ['camera.offline', 'camera.online'];
const SEVERITIES: [string, string][] = [['info', 'Any severity'], ['warning', 'Warning and critical'], ['critical', 'Critical only']];
const NOTIFY = ['telegram', 'slack', 'email'];
const KIND_WORD: Record<string, string> = { email: 'email', telegram: 'Telegram', slack: 'Slack', webhook: 'webhook', kafka: 'Kafka', sftp: 'SFTP' };

/** "Gate 1", "Gate 1 and Gate 2", or "3 cameras" — names while they stay readable. */
function names(ids: string[], list: { id: string; name: string }[], many: string): string {
  const n = ids.map(id => list.find(o => o.id === id)?.name).filter(Boolean) as string[];
  if (n.length !== ids.length || ids.length > 2) return `${ids.length} ${many}`;
  return n.join(' and ');
}

/** The plain sentence shown in the footer and used as the default name. */
export function describe(s: Draft, dest: Destination | undefined, options: Options): string {
  const f = s.filters || {};
  const parts: string[] = [];
  if (s.types.some(t => AI.includes(t))) {
    if (!f.activities?.length) parts.push('AI detections');
    else if (f.activities.length > 2) parts.push(`detections of ${f.activities.length} activities`);
    else parts.push(`${names(f.activities, options.activities, 'activities')} detections`);
  }
  if (s.types.includes(ALERT)) parts.push(f.min_severity === 'critical' ? 'critical alerts' : f.min_severity === 'warning' ? 'warning and critical alerts' : 'alerts');
  const health = HEALTH.filter(t => s.types.includes(t));
  if (health.length === 2) parts.push('camera offline/online changes');
  else if (health.length) parts.push(health[0] === 'camera.offline' ? 'cameras going offline' : 'cameras coming back online');
  if (!parts.length) return 'Choose at least one kind of event.';
  const what = parts.length > 1 ? `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}` : parts[0];
  const where = f.cameras?.length ? ` from ${names(f.cameras, options.cameras, 'cameras')}` : '';
  return `${dest?.name || 'The destination'} will receive ${what}${where}.`;
}

function defaultName(s: Draft, options: Options): string {
  const sentence = describe(s, undefined, options);
  const m = sentence.match(/will receive (.*)\.$/);
  if (!m) return '';
  const text = m[1];
  return (text.charAt(0).toUpperCase() + text.slice(1)).slice(0, 120);
}

/** "All" or "Only these" with a tick list — never a Ctrl-click multi-select. */
function Scope({ label, field, draft, setDraft, items, allText, empty, showLabel = true }: {
  label: string; field: Pick; draft: Draft; setDraft: (f: (d: Draft) => Draft) => void;
  items: { id: string; name: string }[]; allText: string; empty: string; showLabel?: boolean;
}) {
  const chosen = draft.filters[field];
  const only = chosen !== undefined;
  const [query, setQuery] = useState('');
  const set = (v: string[] | undefined) => setDraft(d => {
    const filters = { ...d.filters };
    if (v === undefined) delete filters[field]; else filters[field] = v;
    return { ...d, filters };
  });
  const shown = items.filter(i => i.name.toLowerCase().includes(query.toLowerCase()));
  return <div className="es-scope">
    <div className="es-scope-head" role="radiogroup" aria-label={label}>
      {showLabel && <span className="es-scope-label">{label}</span>}
      <label className="es-radio"><input type="radio" checked={!only} onChange={() => set(undefined)} />{allText}</label>
      <label className="es-radio"><input type="radio" checked={only} disabled={!items.length} onChange={() => set([])} />Only these</label>
    </div>
    {!items.length && <p className="hint">{empty}</p>}
    {only && <div className="es-pick">
      {items.length > 8 && <input className="es-search" aria-label={`Search ${label.toLowerCase()}`} placeholder="Search…" value={query} onChange={e => setQuery(e.target.value)} />}
      <div className="es-pick-list">{shown.map(i => <label key={i.id} className="ei-check">
        <input type="checkbox" checked={chosen!.includes(i.id)}
          onChange={e => set(e.target.checked ? [...chosen!, i.id] : chosen!.filter(x => x !== i.id))} />{i.name}</label>)}
        {!shown.length && <p className="hint">No matches.</p>}</div>
      <p className={`hint${chosen!.length ? '' : ' ei-bad'}`}>{chosen!.length ? `${chosen!.length} selected` : 'Tick at least one, or choose all.'}</p>
    </div>}
  </div>;
}

function Group({ title, blurb, checked, onChange, children }: { title: string; blurb: string; checked: boolean; onChange: (v: boolean) => void; children?: ReactNode }) {
  return <div className={`es-group${checked ? ' on' : ''}`}>
    <label className="es-group-head">
      <input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} />
      <span><b>{title}</b><span>{blurb}</span></span>
    </label>
    {checked && children && <div className="es-group-body">{children}</div>}
  </div>;
}

export function EventSubscriptionModal({ initial, editing, destinations, options, busy, onClose, onSave }: {
  initial: Draft; editing: boolean; destinations: Destination[]; options: Options; busy: boolean;
  onClose: () => void; onSave: (draft: Draft) => void;
}) {
  // Filters stored as [] mean "all"; show them as All.
  const [draft, setDraft] = useState<Draft>(() => {
    const filters = { ...initial.filters };
    (['cameras', 'activities', 'rules'] as Pick[]).forEach(k => { if (!filters[k]?.length) delete filters[k]; });
    return { ...initial, filters };
  });
  const [named, setNamed] = useState(editing && !!initial.name);
  const [tried, setTried] = useState(false);
  const dest = destinations.find(d => d.id === draft.destination_id);
  const has = (t: string) => draft.types.includes(t);
  const setTypes = (add: string[], remove: string[]) => setDraft(d => ({ ...d, types: [...d.types.filter(t => !remove.includes(t)), ...add.filter(t => !d.types.includes(t))] }));
  const aiOn = AI.some(has), alertOn = has(ALERT), healthOn = HEALTH.some(has);
  const name = named ? draft.name : defaultName(draft, options);
  const emptyPick = (['cameras', 'activities', 'rules'] as Pick[]).find(k => draft.filters[k]?.length === 0
    && (k === 'cameras' || (k === 'activities' && aiOn) || (k === 'rules' && alertOn)));
  const problem = !draft.types.length ? 'Choose at least one kind of event.'
    : emptyPick ? `Tick at least one ${emptyPick === 'activities' ? 'activity' : emptyPick.slice(0, -1)}, or choose all.`
    : !name.trim() ? 'Give this subscription a name.' : '';

  function save() {
    setTried(true);
    if (problem) return;
    // Clear filters that belong to event groups that are not selected.
    const filters = { ...draft.filters };
    if (!aiOn) { delete filters.activities; delete filters.include_motion; }
    if (!alertOn) { delete filters.rules; delete filters.min_severity; }
    onSave({ ...draft, name: name.trim(), filters });
  }

  const defaultCooldown = dest && NOTIFY.includes(dest.kind) ? 60 : 0;
  return <Modal open width={680} onClose={onClose} title={editing ? 'Edit subscription' : 'New subscription'}
    footer={<div className="ei-modal-foot">
      <span className={`es-summary${tried && problem ? ' ei-bad' : ''}`} role="status">{tried && problem ? problem : describe(draft, dest, options)}</span>
      <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
      <button type="submit" className="btn-primary" form="ei-sub-form" disabled={busy}>Save subscription</button></div>}>
    <form id="ei-sub-form" onSubmit={e => { e.preventDefault(); save(); }}>
      <p className="es-intro">A subscription decides which events a destination receives. Detection conditions and schedules are set in Alert Rules.</p>

      <label className="form-group es-first"><span className="es-h">Send to</span>
        <select aria-label="Destination" disabled={editing} value={draft.destination_id} onChange={e => setDraft(d => ({ ...d, destination_id: e.target.value }))}>
          {destinations.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select>
        {editing && <span className="hint">To send to a different destination, add a new subscription there.</span>}
      </label>

      <h4 className="es-h">Which events</h4>
      <div className="es-groups">
        <Group title="AI detections" blurb="An AI activity is detected on a camera, such as an intrusion or loitering." checked={aiOn}
          onChange={v => setTypes(v ? ['ai.event.created'] : [], v ? [] : AI)}>
          <div className="es-row" role="radiogroup" aria-label="When to send detections">
            <label className="es-radio"><input type="radio" checked={!has('ai.event.closed')} onChange={() => setTypes(['ai.event.created'], ['ai.event.closed'])} />When it starts</label>
            <label className="es-radio"><input type="radio" checked={has('ai.event.closed')} onChange={() => setTypes(AI, [])} />When it starts and again when it ends</label>
          </div>
          <Scope label="Activities" field="activities" draft={draft} setDraft={setDraft} items={options.activities} allText="All activities" empty="No activities are configured yet." />
          <label className="ei-check"><input type="checkbox" checked={!!draft.filters.include_motion} onChange={e => setDraft(d => ({ ...d, filters: { ...d.filters, include_motion: e.target.checked } }))} />Include plain motion (left out by default, it is frequent)</label>
        </Group>
        <Group title="Alerts" blurb="An Alert Rule fires. Rules combine detections with schedules and arming." checked={alertOn}
          onChange={v => setTypes(v ? [ALERT] : [], v ? [] : [ALERT])}>
          <Scope label="Rules" field="rules" draft={draft} setDraft={setDraft} items={options.rules} allText="All rules" empty="No Alert Rules exist yet." />
          <div className="es-row" role="radiogroup" aria-label="Minimum severity">
            <span className="es-scope-label">Severity</span>
            {SEVERITIES.map(([k, label]) => <label key={k} className="es-radio"><input type="radio" checked={(draft.filters.min_severity || 'info') === k}
              onChange={() => setDraft(d => ({ ...d, filters: { ...d.filters, min_severity: k } }))} />{label}</label>)}
          </div>
        </Group>
        <Group title="Camera health" blurb="A camera goes offline or comes back online." checked={healthOn}
          onChange={v => setTypes(v ? HEALTH : [], v ? [] : HEALTH)}>
          <div className="es-row">
            <label className="ei-check"><input type="checkbox" checked={has('camera.offline')} onChange={e => setTypes(e.target.checked ? ['camera.offline'] : [], e.target.checked ? [] : ['camera.offline'])} />Goes offline</label>
            <label className="ei-check"><input type="checkbox" checked={has('camera.online')} onChange={e => setTypes(e.target.checked ? ['camera.online'] : [], e.target.checked ? [] : ['camera.online'])} />Comes back online</label>
          </div>
        </Group>
      </div>

      <h4 className="es-h">From which cameras</h4>
      <Scope label="Cameras" field="cameras" draft={draft} setDraft={setDraft} items={options.cameras} allText="All cameras" empty="No cameras are registered yet." showLabel={false} />

      <label className="form-group es-narrow"><span className="es-h">Skip repeats within</span>
        <span className="ei-inline"><input type="number" aria-label="Skip repeats within (seconds)" min={0} max={86400} value={draft.cooldown_s ?? ''} placeholder={String(defaultCooldown)}
          onChange={e => setDraft(d => ({ ...d, cooldown_s: e.target.value === '' ? null : Number(e.target.value) }))} /><span className="es-unit">seconds</span></span>
        <span className="hint">The same camera and activity is sent once per window. Blank uses the default for {dest ? `${KIND_WORD[dest.kind]} destinations` : 'this destination'}: {defaultCooldown ? `${defaultCooldown} seconds` : 'none, every event is sent'}. 0 sends every event.</span>
      </label>

      <label className="form-group"><span className="es-h">Name</span>
        <input aria-label="Subscription name" maxLength={120} value={name} onChange={e => { setNamed(true); setDraft(d => ({ ...d, name: e.target.value })); }} />
        {!named && <span className="hint">Filled in from your choices. Type to change it.</span>}
      </label>
    </form>
  </Modal>;
}
