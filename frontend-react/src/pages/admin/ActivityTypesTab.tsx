/**
 * ActivityTypesTab.tsx — Administration → Activity types: the activities the
 * CPU analytics engine provides.
 *
 * NOTHING HERE CREATES AN ACTIVITY OR ITS SETTINGS. Which activities exist,
 * whether they run, which zones they take and every setting's definition come
 * from the analytics engine's code: it publishes its registry, camera-mgmt
 * copies it into the catalog, and this page shows it. An administrator names
 * and colours each activity for this site and can read exactly what its
 * settings are; the values for a camera are set under that camera's AI Config.
 *
 * Save sends only key, label and colour (PUT /cameras/analytics/types); the API
 * refuses a key the engine does not know and ignores anything else.
 */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import { STATUS_LABEL, type ActivityMeta, type ParamField } from '@/pages/config/analytics/activities';
import { countUsage } from './activityTypeBits';

interface Draft {
  key: string;
  label: string;
  color: string;
}

const ZONE_RULE_TEXT: Record<string, string> = {
  optional: 'Watches its zones; with no zone, the whole frame',
  required: 'Needs at least one zone',
  tripwire: 'Watches tripwires',
};

/** One line describing a setting as the engine defines it. */
export function describeField(f: ParamField): string {
  const def = Array.isArray(f.default) ? f.default.join(', ') : String(f.default ?? '—');
  const parts = [`default ${def}${f.unit ? ` ${f.unit}` : ''}`];
  if (f.min != null || f.max != null) parts.push(`range ${f.min ?? '…'}–${f.max ?? '…'}`);
  if (f.options?.length) parts.push(`choices ${f.options.join(', ')}`);
  return parts.join(' · ');
}

export function ActivityTypesTab() {
  const toast = useToast();
  const [stored, setStored] = useState<ActivityMeta[] | null>(null);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [usage, setUsage] = useState<Record<string, number> | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const seed = (types: ActivityMeta[]) =>
    setDrafts(types.map(t => ({ key: t.key, label: t.label, color: t.color })));

  async function load() {
    try {
      const types = await apiFetch<ActivityMeta[]>('/cameras/analytics/types');
      setStored(types);
      seed(types);
      setErr('');
    } catch (e: any) { setErr(e.message); }
    try {
      setUsage(countUsage(await apiFetch<Camera[]>('/cameras?limit=1000')));
    } catch { setUsage(null); }
  }

  useEffect(() => { load(); }, []);

  const set = (key: string, patch: Partial<Draft>) =>
    setDrafts(ds => ds.map(d => (d.key === key ? { ...d, ...patch } : d)));

  const dirty = !!stored && drafts.some((d, i) =>
    d.label.trim() !== stored[i]?.label || d.color !== stored[i]?.color);
  const blank = drafts.some(d => !d.label.trim());

  async function save() {
    setBusy(true);
    try {
      const saved = await apiFetch<ActivityMeta[]>('/cameras/analytics/types', {
        method: 'PUT',
        body: JSON.stringify({ types: drafts.map(d => ({ key: d.key, label: d.label.trim(), color: d.color })) }),
      });
      setStored(saved);
      seed(saved);
      toast('Activity types updated');
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }

  if (err) return <div className="card"><p style={{ color: 'var(--red)', fontSize: 13 }}>{err}</p></div>;
  if (!stored) return <div className="card"><p style={{ color: 'var(--dim)', fontSize: 13 }}>Loading…</p></div>;

  const byKey = Object.fromEntries(stored.map(t => [t.key, t]));

  return (
    <div className="card" style={{ maxWidth: 760 }}>
      <div className="card-title">Activity types</div>
      <div className="d-hint" style={{ marginTop: 6, marginBottom: 14 }}>
        The activities the analytics engine provides. Which activities exist, and what each one's settings
        are, come from the engine itself — you can rename and recolour them here. A camera's values are set
        under its AI Config. Only <b>Available</b> activities can be added to a camera.
      </div>

      {!stored.length && (
        <div className="d-hint" style={{ marginBottom: 12 }}>
          No activities yet — the catalog fills in once the analytics engine has been reached.
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {drafts.map(d => {
          const meta = byKey[d.key];
          const status = meta?.status ?? 'unregistered';
          const fields = (meta?.params_schema || []).filter(f => f.configurable !== false);
          const used = usage ? (usage[d.key] || 0) : null;
          return (
            <div key={d.key} data-type-key={d.key} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <input type="color" value={d.color} onChange={e => set(d.key, { color: e.target.value })}
                       title="Colour used for this activity across the UI" aria-label={`${d.key} colour`}
                       style={{ width: 34, height: 30, padding: 0, border: '1px solid var(--border2)', borderRadius: 6, flexShrink: 0 }} />
                <input value={d.label} onChange={e => set(d.key, { label: e.target.value })} maxLength={120}
                       aria-label={`${d.key} name`}
                       style={{ flex: 1, fontSize: 12.5, ...(d.label.trim() ? null : { borderColor: 'var(--red)' }) }} />
                <span style={{ width: 150, fontSize: 10.5, color: 'var(--dim)', fontFamily: 'var(--mono)',
                               overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={d.key}>
                  {d.key}
                </span>
                <span className={`badge ${status === 'available' ? 'badge-green' : 'badge-gray'}`}
                      style={{ flexShrink: 0 }}>{STATUS_LABEL[status]}</span>
                <span style={{ width: 70, fontSize: 11.5, flexShrink: 0, color: used ? 'var(--muted)' : 'var(--dim)' }}>
                  {used === null ? '—' : used === 0 ? 'unused' : `used by ${used}`}
                </span>
                <button className="btn-ghost btn-sm" style={{ fontSize: 11, flexShrink: 0, minWidth: 92 }}
                        aria-expanded={open === d.key} onClick={() => setOpen(open === d.key ? null : d.key)}>
                  {open === d.key ? '▾' : '▸'} {fields.length ? `${fields.length} setting${fields.length === 1 ? '' : 's'}` : 'No settings'}
                </button>
              </div>

              {open === d.key && (
                <div style={{ marginLeft: 42, display: 'flex', flexDirection: 'column', gap: 6, padding: '10px 12px',
                              borderRadius: 8, background: 'var(--surface2)', border: '1px solid var(--border)' }}>
                  {meta?.description && <div style={{ fontSize: 12.5 }}>{meta.description}</div>}
                  <div className="cfg-hint">
                    {ZONE_RULE_TEXT[meta?.zone_rule ?? ''] ?? 'Zone rule not known yet'}
                    {meta?.definition_version ? ` · definition v${meta.definition_version}` : ''}
                  </div>
                  {fields.length === 0 ? (
                    <div className="cfg-hint">No settings beyond the schedule.</div>
                  ) : fields.map(f => (
                    <div key={f.key} className="act-row" style={{ alignItems: 'baseline', flexWrap: 'wrap' }}>
                      <span className="act-label" style={{ minWidth: 190 }}>{f.label}</span>
                      <span style={{ fontSize: 12, color: 'var(--text2)' }}>{describeField(f)}</span>
                      {f.help && <span style={{ fontSize: 11.5, color: 'var(--dim)', flexBasis: '100%' }}>{f.help}</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="form-actions" style={{ marginTop: 18 }}>
        <button className="btn-primary" disabled={busy || !dirty || blank} onClick={save}>
          {busy ? 'Saving…' : dirty ? 'Save names & colours' : 'Saved'}
        </button>
        <button className="btn-ghost" disabled={busy || !dirty} onClick={() => seed(stored)}>Discard changes</button>
        {blank && <span style={{ fontSize: 11.5, color: 'var(--red)' }}>Every activity needs a name</span>}
      </div>
    </div>
  );
}
