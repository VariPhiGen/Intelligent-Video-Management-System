/**
 * LabelClassesTab — the fleet label taxonomy an administrator owns.
 *
 * RETIRE, DO NOT DELETE is the rule this screen has to teach. Annotations name
 * a class by key and outlive a change of mind about the taxonomy, so the server
 * refuses to delete a class anything references (409) and `active = false` is
 * the supported way to take one out of circulation. The row therefore offers
 * whichever action can actually succeed, and never both.
 *
 * THE KEY IS FOREVER, EVERYTHING ELSE IS A DISPLAY CHOICE. `key` is what
 * annotations store and what COCO exports carry, so PATCH does not accept it
 * and this screen only lets you set it at birth — validated here against the
 * server's own `^[a-z0-9_]+$` so a stray capital becomes an inline hint rather
 * than a 422. `label`, `color` and `sort_order` are editable in place, which is
 * the whole reason they are separate columns from the key.
 *
 * Edits batch behind one Save, the way Activity types does it — the two screens
 * are the same shape of problem and should not feel different. While an edit is
 * pending the per-row actions are disabled: retire-then-reload would silently
 * throw the edit away, and a button that loses your typing is worse than one
 * that greys out and says why.
 */
import { useEffect, useMemo, useState } from 'react';
import { useToast } from '@/components/Toast';
import {
  createLabelClass, deleteLabelClass, listLabelClasses, updateLabelClass,
  type LabelClass,
} from '@/lib/labelClasses';

/** The server's rule, restated so a bad key never becomes a 422. */
const KEY_RE = /^[a-z0-9_]+$/;

const BLANK = { key: '', label: '', color: '#ff8800', sort_order: '', description: '' };
type NewDraft = typeof BLANK;

/** An edit in flight: only the three fields PATCH will accept. */
interface Edit {
  label: string;
  color: string;
  sort_order: number;
}

/** `Forklift truck` → `forklift_truck`, so nobody has to learn the key rule. */
export function slugify(label: string): string {
  return label.toLowerCase().replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '').slice(0, 64);
}

export function LabelClassesTab() {
  const toast = useToast();
  const [classes, setClasses] = useState<LabelClass[] | null>(null);
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [draft, setDraft] = useState<NewDraft>(BLANK);
  const [adding, setAdding] = useState(false);
  const [showRetired, setShowRetired] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const seed = (all: LabelClass[]) => setEdits(Object.fromEntries(all.map(
    c => [c.key, { label: c.label, color: c.color, sort_order: c.sort_order }])));

  async function reload() {
    try {
      const all = await listLabelClasses();
      setClasses(all);
      seed(all);
      setErr('');
    } catch (e: any) {
      setErr(e?.message || 'Could not load label classes');
    }
  }

  useEffect(() => { void reload(); }, []);

  /** Which rows differ from what the server last told us. */
  const dirtyKeys = useMemo(() => (classes || []).filter(c => {
    const e = edits[c.key];
    return e && (e.label.trim() !== c.label || e.color !== c.color
                 || e.sort_order !== c.sort_order);
  }).map(c => c.key), [classes, edits]);

  const blankLabel = dirtyKeys.some(k => !edits[k]?.label.trim());
  const dirty = dirtyKeys.length > 0;

  const set = (key: string, patch: Partial<Edit>) =>
    setEdits(m => ({ ...m, [key]: { ...m[key], ...patch } }));

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      toast(ok);
      await reload();
    } catch (e: any) {
      toast(e?.message || 'That did not work', 'err');
    } finally {
      setBusy(false);
    }
  }

  /** One PATCH per changed row — the API has no batch, and a partial failure
   *  should still leave the rows that succeeded saved. */
  const saveEdits = () => act(async () => {
    for (const key of dirtyKeys) {
      const e = edits[key];
      await updateLabelClass(key, {
        label: e.label.trim(), color: e.color, sort_order: e.sort_order,
      });
    }
  }, dirtyKeys.length === 1 ? 'Class updated' : `${dirtyKeys.length} classes updated`);

  // The key is what the server will store, so it is validated on the value the
  // form will actually send — trimmed, never the raw keystrokes.
  const key = draft.key.trim();
  const keyBad = key !== '' && !KEY_RE.test(key);
  const keyTaken = !!classes?.some(c => c.key === key);
  const canAdd = !!key && !keyBad && !keyTaken && !!draft.label.trim();

  const addClass = () => act(async () => {
    await createLabelClass({
      key,
      label: draft.label.trim(),
      color: draft.color,
      sort_order: draft.sort_order === '' ? 0 : Number(draft.sort_order),
      description: draft.description.trim() || null,
    });
    setDraft(BLANK);
    setAdding(false);
  }, 'Class added');

  const retired = (classes || []).filter(c => !c.active);
  const rows = (classes || [])
    .filter(c => showRetired || c.active)
    .slice()
    .sort((a, b) => a.sort_order - b.sort_order || a.key.localeCompare(b.key));

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <div className="card-title">Label classes</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {retired.length > 0 && (
              <label style={{ display: 'flex', alignItems: 'center', gap: 6,
                              fontSize: 12.5, color: 'var(--muted)' }}>
                <input type="checkbox" checked={showRetired}
                       onChange={e => setShowRetired(e.target.checked)} />
                Show retired ({retired.length})
              </label>
            )}
            <button className="btn-primary btn-sm" data-action="new"
                    disabled={busy} onClick={() => setAdding(a => !a)}>
              {adding ? 'Cancel' : '+ Add class'}
            </button>
          </div>
        </div>

        <div className="panel-body flush">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 52 }}>Colour</th>
                  <th>Name</th>
                  <th style={{ width: 170 }}>Key</th>
                  <th style={{ width: 80 }}>Order</th>
                  <th style={{ width: 110 }}>Annotations</th>
                  <th style={{ width: 100 }}>State</th>
                  <th style={{ width: 96 }} />
                </tr>
              </thead>
              <tbody>
                {err && <tr><td colSpan={7}><div className="empty">{err}</div></td></tr>}
                {!err && classes === null && (
                  <tr><td colSpan={7}><div className="empty">
                    <span className="spinner" />
                    <span style={{ marginLeft: 8 }}>Loading label classes…</span>
                  </div></td></tr>
                )}
                {!err && classes !== null && rows.length === 0 && (
                  <tr><td colSpan={7}>
                    <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                      <div className="glyph">🏷</div>
                      <h4>No label classes yet</h4>
                      <p>
                        A class is what an operator may call the box they draw on
                        Playback. Add the first one and it appears in the annotate
                        picker straight away.
                      </p>
                    </div>
                  </td></tr>
                )}
                {rows.map(c => {
                  const e = edits[c.key];
                  if (!e) return null;
                  const rowDirty = dirtyKeys.includes(c.key);
                  return (
                    <tr key={c.key} data-class-key={c.key}
                        style={c.active ? undefined : { opacity: 0.6 }}>
                      <td>
                        <input type="color" value={e.color} aria-label={`${c.key} colour`}
                               title="Used for this class everywhere it is drawn"
                               onChange={ev => set(c.key, { color: ev.target.value })}
                               style={{ width: 32, height: 28, padding: 0,
                                        border: '1px solid var(--border2)',
                                        borderRadius: 6 }} />
                      </td>
                      <td>
                        <input value={e.label} maxLength={120}
                               aria-label={`${c.key} name`}
                               onChange={ev => set(c.key, { label: ev.target.value })}
                               style={{ width: '100%', fontSize: 12.5,
                                        ...(e.label.trim() ? null : { borderColor: 'var(--red)' }) }} />
                        {c.description && (
                          <div style={{ fontSize: 11.5, color: 'var(--dim)', marginTop: 3 }}>
                            {c.description}
                          </div>
                        )}
                      </td>
                      <td className="mono" style={{ fontSize: 12, color: 'var(--text2)' }}
                          title="What annotations store and exports carry — it cannot be renamed">
                        {c.key}
                      </td>
                      <td>
                        <input type="number" value={e.sort_order}
                               aria-label={`${c.key} order`}
                               onChange={ev => set(c.key, {
                                 sort_order: Number(ev.target.value) || 0 })}
                               style={{ width: 60, fontSize: 12.5 }} />
                      </td>
                      <td style={{ fontSize: 12.5,
                                   color: c.annotation_count ? 'var(--text2)' : 'var(--dim)' }}>
                        {c.annotation_count || 'none'}
                      </td>
                      <td>
                        {c.active
                          ? <span className="badge badge-green"><span className="badge-dot" />Active</span>
                          : <span className="badge badge-gray"><span className="badge-dot" />Retired</span>}
                      </td>
                      <td>
                        {!c.active ? (
                          <button className="btn-subtle btn-sm" data-action="restore"
                                  disabled={busy || rowDirty}
                                  title={rowDirty ? 'Save or discard your edit first'
                                                  : 'Put this class back in the picker'}
                                  onClick={() => act(
                                    () => updateLabelClass(c.key, { active: true }),
                                    'Class restored')}>
                            Restore
                          </button>
                        ) : c.annotation_count > 0 ? (
                          <button className="btn-subtle btn-sm" data-action="retire"
                                  disabled={busy || rowDirty}
                                  title={rowDirty ? 'Save or discard your edit first'
                                    : `${c.annotation_count} annotation(s) name this class, so it cannot be deleted — retiring takes it out of the picker and keeps them readable`}
                                  onClick={() => act(
                                    () => updateLabelClass(c.key, { active: false }),
                                    'Class retired')}>
                            Retire
                          </button>
                        ) : (
                          <button className="btn-subtle btn-sm" data-action="delete"
                                  disabled={busy || rowDirty}
                                  title={rowDirty ? 'Save or discard your edit first'
                                                  : 'Nothing names this class yet, so it can be removed outright'}
                                  onClick={() => act(() => deleteLabelClass(c.key),
                                                     'Class deleted')}>
                            Delete
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {dirty && (
          <div className="panel-head" style={{ borderTop: '1px solid var(--border)',
                                               borderBottom: 'none' }}>
            <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>
              {dirtyKeys.length === 1 ? '1 class edited' : `${dirtyKeys.length} classes edited`}
              {blankLabel && <span style={{ color: 'var(--red)' }}> · every class needs a name</span>}
            </span>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn-ghost btn-sm" disabled={busy}
                      onClick={() => classes && seed(classes)}>Discard</button>
              <button className="btn-primary btn-sm" data-action="save"
                      disabled={busy || blankLabel} onClick={saveEdits}>
                {busy ? 'Saving…' : 'Save changes'}
              </button>
            </div>
          </div>
        )}
      </div>

      {adding && (
        <div className="card" style={{ maxWidth: 620 }}>
          <div className="card-title">Add a label class</div>
          <div className="d-hint" style={{ marginBottom: 14 }}>
            The key is what annotations store and what exports carry, so it is
            chosen once and never renamed. The name and colour are display
            choices you can change later.
          </div>

          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="lc-label">Name</label>
              <input id="lc-label" value={draft.label} maxLength={120}
                     placeholder="Forklift"
                     onChange={e => {
                       const label = e.target.value;
                       // The key follows the name until it is touched, then it
                       // is the operator's: an auto-slug that keeps overwriting
                       // a deliberate key is the worse surprise.
                       setDraft(d => ({
                         ...d, label,
                         key: d.key === slugify(d.label) || d.key === ''
                           ? slugify(label) : d.key,
                       }));
                     }} />
            </div>
            <div className="form-group">
              <label htmlFor="lc-key">Key</label>
              <input id="lc-key" value={draft.key} maxLength={64}
                     className="mono" placeholder="forklift"
                     aria-invalid={keyBad || keyTaken}
                     onChange={e => setDraft(d => ({ ...d, key: e.target.value }))}
                     style={(keyBad || keyTaken) ? { borderColor: 'var(--red)' } : undefined} />
              {keyBad && (
                <span style={{ fontSize: 11.5, color: 'var(--red)' }}>
                  Lowercase letters, digits and underscores only
                </span>
              )}
              {keyTaken && (
                <span style={{ fontSize: 11.5, color: 'var(--red)' }}>
                  A class with this key already exists
                </span>
              )}
            </div>
            <div className="form-group">
              <label htmlFor="lc-color">Colour</label>
              <input id="lc-color" type="color" value={draft.color}
                     onChange={e => setDraft(d => ({ ...d, color: e.target.value }))}
                     style={{ width: 56, height: 32, padding: 0,
                              border: '1px solid var(--border2)', borderRadius: 6 }} />
            </div>
            <div className="form-group">
              <label htmlFor="lc-order">Order</label>
              <input id="lc-order" type="number" value={draft.sort_order}
                     placeholder="0"
                     onChange={e => setDraft(d => ({ ...d, sort_order: e.target.value }))}
                     style={{ width: 80 }} />
              <span className="d-hint" style={{ marginTop: 0 }}>
                Lowest first in the picker
              </span>
            </div>
            <div className="form-group full">
              <label htmlFor="lc-desc">Description <span style={{ color: 'var(--dim)' }}>(optional)</span></label>
              <input id="lc-desc" value={draft.description} maxLength={500}
                     placeholder="What counts as this class, for whoever draws it"
                     onChange={e => setDraft(d => ({ ...d, description: e.target.value }))} />
            </div>
          </div>

          <div className="form-actions" style={{ marginTop: 18 }}>
            <button className="btn-primary" data-action="create"
                    disabled={busy || !canAdd} onClick={addClass}>
              {busy ? 'Adding…' : 'Add class'}
            </button>
            <button className="btn-ghost" disabled={busy}
                    onClick={() => { setDraft(BLANK); setAdding(false); }}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </>
  );
}
