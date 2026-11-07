/**
 * PresetsPanel — a PTZ camera's presets as VMS records them (services/ptz_presets): search, filter, sort,
 * picture grid or compact list, drag to reorder (the order of the live view's recall buttons), bulk delete,
 * paged. A camera holds up to a few hundred, so this filters in the browser.
 *
 * A rename is VMS-only (the camera's SetPreset would also re-store the CURRENT position); Restore puts a preset
 * the camera lost back from the stored position.
 */
import { useMemo, useState } from 'react';
import { AuthImg } from '@/components/AuthImage';
import { Pager } from '@/components/Pager';
import { useToast } from '@/components/Toast';
import { ptzApi, useZoomText, type PtzPosition } from '@/lib/ptz';
import type { PtzCapabilities, PtzPreset } from '@/lib/types';
import { aiText, reviewText } from '@/pages/config/analytics/analyticsTarget';

const PAGE = 24;
type Sort = 'order' | 'name' | 'newest' | 'used' | 'last';
type Flag = 'home' | 'missing' | 'nopic' | 'unused';
const num = (v: number | null | undefined) => (v == null ? '—' : v.toFixed(3));

export function PresetsPanel({ cameraId, caps, presets, cameraError, position, canPtz, canManage, speed, reload, onPeek }: {
  cameraId: string;
  /** for showing zoom as × / % (useZoomText) */
  caps?: PtzCapabilities | null;
  presets: PtzPreset[] | null;
  cameraError: string | null;
  position: PtzPosition | null;
  canPtz: boolean;
  canManage: boolean;
  speed: number;
  reload: () => void;
  /** Hovering a preset → its saved picture over the live one (PtzTab); null when the pointer leaves. */
  onPeek?: (p: PtzPreset | null) => void;
}) {
  const zoomFmt = useZoomText(cameraId, caps);
  // Visit counts are movement history: the server sends null to anyone without "PTZ movement history".
  const hasVisits = !(presets || []).some(p => p.visits === null);
  const toast = useToast();
  const [q, setQ] = useState('');
  const [flags, setFlags] = useState<Flag[]>([]);
  const [sort, setSort] = useState<Sort>('order');
  const [view, setView] = useState<'grid' | 'list'>(() => {
    try { return (localStorage.getItem('vms.ptz.presetView') as 'grid' | 'list') || 'grid'; } catch { return 'grid'; }
  });
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState('');
  const [drag, setDrag] = useState<string | null>(null);

  function setViewSaved(v: 'grid' | 'list') {
    setView(v);
    try { localStorage.setItem('vms.ptz.presetView', v); } catch { /* private window */ }
  }

  // Overwrite a preset with where the camera points now — ONVIF SetPreset with its token, so the token (and every
  // patrol stop that uses it) stays; VMS's record gets the new position and picture. Asked first: it can't be undone.
  const update = (p: PtzPreset) => {
    const zones = (p.ai?.zones || p.ai?.tripwires || p.ai?.rules)
      ? `\n\nIts zones are lined up with the new picture: moved with it if it slid a little; otherwise its rules pause until you check them.`
      : '';
    if (!window.confirm(`Set “${p.name || p.token}” to where the camera is pointing now?${zones}`)) return;
    void run(`“${p.name || p.token}” updated to this view`, () => ptzApi.savePreset(cameraId, p.name || p.token, p.token));
  };

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    try { await fn(); toast(label); reload(); } catch (e: any) { toast(e.message, 'err'); } finally { setBusy(false); }
  }

  const shown = useMemo(() => {
    let list = [...(presets || [])];
    const needle = q.trim().toLowerCase();
    if (needle) list = list.filter(p => `${p.name || ''} ${p.token}`.toLowerCase().includes(needle));
    if (flags.includes('home')) list = list.filter(p => p.is_home);
    if (flags.includes('missing')) list = list.filter(p => p.missing_on_camera);
    if (flags.includes('nopic')) list = list.filter(p => !p.has_thumbnail);
    if (flags.includes('unused')) list = list.filter(p => !p.visits);
    const by: Record<Sort, (a: PtzPreset, b: PtzPreset) => number> = {
      order: (a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0),
      name: (a, b) => (a.name || a.token).localeCompare(b.name || b.token, undefined, { numeric: true }),
      newest: (a, b) => String(b.created_at || b.thumbnail_at || '').localeCompare(String(a.created_at || a.thumbnail_at || '')),
      used: (a, b) => (b.visits || 0) - (a.visits || 0),
      last: (a, b) => String(b.last_visit || '').localeCompare(String(a.last_visit || '')),
    };
    return list.sort(by[sort]);
  }, [presets, q, flags, sort]);

  const pageItems = shown.slice(page * PAGE, (page + 1) * PAGE);
  const canReorder = canManage && sort === 'order' && !q && flags.length === 0;
  const atToken = position?.state === 'at_preset' ? position.preset_token : null;

  function toggleFlag(f: Flag) { setPage(0); setFlags(v => (v.includes(f) ? v.filter(x => x !== f) : [...v, f])); }

  async function dropOn(target: string) {
    if (!drag || drag === target || !presets) return;
    const order = [...presets].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0)).map(p => p.token);
    const from = order.indexOf(drag);
    order.splice(from, 1);
    order.splice(order.indexOf(target), 0, drag);
    setDrag(null);
    await run('Order saved', () => ptzApi.orderPresets(cameraId, order));
  }

  async function bulkDelete() {
    const toks = [...selected];
    if (!toks.length || !window.confirm(`Delete ${toks.length} preset(s) from the camera?\n\nPatrol stops on them are skipped from now on (never moved to another preset that later gets the same number).`)) return;
    setBusy(true);
    let failed = 0;
    for (const t of toks) {
      try { await ptzApi.deletePreset(cameraId, t); } catch { failed += 1; }
    }
    setBusy(false);
    setSelected(new Set());
    toast(failed ? `${toks.length - failed} deleted, ${failed} failed` : `${toks.length} preset(s) deleted`, failed ? 'err' : undefined);
    reload();
  }

  const go = (p: PtzPreset) => run(`Moving to ${p.name || p.token}`, () => ptzApi.gotoPreset(cameraId, p.token, speed));

  return (
    <div>
      {canManage && (
        <form className="list-toolbar" onSubmit={e => { e.preventDefault(); if (!name.trim()) return;
          void run(`Preset “${name.trim()}” saved`, () => ptzApi.savePreset(cameraId, name.trim())).then(() => setName('')); }}>
          <input value={name} onChange={e => setName(e.target.value)} placeholder="Name this view" maxLength={64}
                 style={{ width: 220 }} />
          <button className="btn-primary btn-sm" type="submit" disabled={busy || !name.trim()}
                  title="Stores where the camera is pointing RIGHT NOW — aim it with the live picture first">
            Save current view as a preset
          </button>
        </form>
      )}

      <div className="list-toolbar">
        <input type="search" value={q} onChange={e => { setQ(e.target.value); setPage(0); }} placeholder="Search presets…" />
        {((hasVisits ? ['home', 'missing', 'nopic', 'unused'] : ['home', 'missing', 'nopic']) as Flag[]).map(f => (
          <span key={f} className={`chip-toggle${flags.includes(f) ? ' on' : ''}`} onClick={() => toggleFlag(f)}>
            {{ home: '⌂ Home', missing: 'Missing on camera', nopic: 'No picture', unused: 'Never visited' }[f]}
          </span>
        ))}
        <select value={sort} onChange={e => setSort(e.target.value as Sort)} aria-label="Sort">
          <option value="order">Your order</option>
          <option value="name">Name</option>
          <option value="newest">Newest</option>
          {hasVisits && <option value="used">Most visited</option>}
          {hasVisits && <option value="last">Recently visited</option>}
        </select>
        <div style={{ flex: 1 }} />
        {view === 'list' && canManage && selected.size > 0 && (
          <button className="btn-subtle btn-sm" disabled={busy} onClick={bulkDelete}>Delete {selected.size} selected</button>
        )}
        <span className={`chip-toggle${view === 'grid' ? ' on' : ''}`} onClick={() => setViewSaved('grid')}>▦ Pictures</span>
        <span className={`chip-toggle${view === 'list' ? ' on' : ''}`} onClick={() => setViewSaved('list')}>☰ List</span>
      </div>

      {cameraError && (
        <div style={{ fontSize: 11.5, color: 'var(--warn, #f59e0b)', marginBottom: 8 }}>
          The camera didn’t answer just now ({cameraError}) — showing the presets VMS has on record.
        </div>
      )}

      {presets === null ? <div style={{ fontSize: 12, color: 'var(--dim)' }}>Reading presets from the camera…</div>
        : presets.length === 0 ? <div style={{ fontSize: 12, color: 'var(--dim)' }}>No presets on this camera yet — aim it with the live picture (click to centre, drag to zoom), name the view above and save.</div>
        : shown.length === 0 ? <div style={{ fontSize: 12, color: 'var(--dim)' }}>No preset matches.</div>
        : view === 'grid' ? (
          <div className="ptz-preset-grid">
            {pageItems.map(p => (
              <PresetCard key={p.token} cameraId={cameraId} p={p} at={atToken === p.token}
                          canPtz={canPtz} canManage={canManage} busy={busy} onGo={() => go(p)} onPeek={onPeek}
                          onDelete={() => { if (window.confirm(`Delete preset “${p.name || p.token}”${p.missing_on_camera ? '' : ' from the camera'}?\n\nPatrol stops on it are skipped from now on (never moved to another preset that later gets the same number).${(p.ai?.zones || p.ai?.tripwires || p.ai?.rules) ? '\n\nIts zones and rules are kept under Zones & Analytics → “Deleted presets”, to copy onto another preset.' : ''}`))
                            void run('Preset deleted', () => ptzApi.deletePreset(cameraId, p.token)); }}
                          onRename={n => run('Preset renamed', () => ptzApi.patchPreset(cameraId, p.token, { name: n }))}
                          onHome={() => run(p.is_home ? 'Home unset' : `${p.name || p.token} is now Home`,
                            () => ptzApi.patchPreset(cameraId, p.token, { is_home: !p.is_home }))}
                          onPicture={() => run('Picture updated', () => ptzApi.refreshThumbnail(cameraId, p.token))}
                          onRestore={() => run(`${p.name || p.token} put back on the camera`, () => ptzApi.restorePreset(cameraId, p.token))}
                          onUpdate={() => update(p)} />
            ))}
          </div>
        ) : (
          <table className="tbl ptz-preset-list" style={{ fontSize: 12 }}>
            <thead><tr>
              {canManage && <th style={{ width: 24 }}>
                <input type="checkbox" aria-label="Select all on this page"
                       checked={pageItems.length > 0 && pageItems.every(p => selected.has(p.token))}
                       onChange={e => setSelected(s => { const n = new Set(s); pageItems.forEach(p => (e.target.checked ? n.add(p.token) : n.delete(p.token))); return n; })} />
              </th>}
              {canReorder && <th style={{ width: 18 }} />}
              <th>Name</th><th>Pan / tilt</th><th>Zoom</th>{hasVisits && <><th>Visits</th><th>Last visit</th></>}<th>Saved by</th><th />
            </tr></thead>
            <tbody>
              {pageItems.map(p => (
                <tr key={p.token} className={drag === p.token ? 'dragging' : ''}
                    draggable={canReorder} onDragStart={() => setDrag(p.token)} onDragEnd={() => setDrag(null)}
                    onDragOver={e => { if (canReorder) e.preventDefault(); }} onDrop={() => void dropOn(p.token)}
                    onMouseEnter={() => onPeek?.(p)} onMouseLeave={() => onPeek?.(null)}>
                  {canManage && <td><input type="checkbox" checked={selected.has(p.token)} aria-label={`Select ${p.name || p.token}`}
                    onChange={e => setSelected(s => { const n = new Set(s); if (e.target.checked) n.add(p.token); else n.delete(p.token); return n; })} /></td>}
                  {canReorder && <td className="drag" title="Drag to reorder">⠿</td>}
                  <td>
                    {p.is_home ? '⌂ ' : ''}{p.name || p.token}
                    {atToken === p.token && <span style={{ marginLeft: 6, fontSize: 11 }}>📍 here now</span>}
                    {p.missing_on_camera && <span style={{ marginLeft: 6, fontSize: 11, color: 'var(--warn, #f59e0b)' }}>missing on camera</span>}
                    {(p.ai?.rules || p.ai?.zones || p.ai?.tripwires) ? (
                      <span title="This preset's own AI (Zones & Analytics / AI Config → View)"
                            style={{ marginLeft: 6, fontSize: 11, color: 'var(--accent, #2563eb)' }}>AI: {aiText(p)}</span>
                    ) : null}
                    {p.review && (
                      <span title={`${p.review.note} (${p.review.reason}). Check in Zones & Analytics → View: ${p.name || p.token}`}
                            style={{ marginLeft: 6, fontSize: 11, color: p.review.paused ? 'var(--warn, #f59e0b)' : 'var(--text2)' }}>
                        ⚠ {reviewText(p)}
                      </span>
                    )}
                  </td>
                  <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>{num(p.pan)} / {num(p.tilt)}</td>
                  <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }} title={p.zoom == null ? undefined : `zoom ${num(p.zoom)}`}>{zoomFmt(p.zoom)}</td>
                  {hasVisits && <td>{p.visits ?? 0}</td>}
                  {hasVisits && <td style={{ whiteSpace: 'nowrap' }}>{p.last_visit ? new Date(p.last_visit).toLocaleString() : '—'}</td>}
                  <td>{p.created_by || '—'}</td>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <button className="btn-subtle btn-xs" disabled={!canPtz || busy || p.missing_on_camera} onClick={() => go(p)}>Go</button>
                    {canManage && !p.missing_on_camera && (
                      <button className="btn-subtle btn-xs" disabled={busy} style={{ marginLeft: 4 }}
                              title="Set this preset to where the camera is pointing now" onClick={() => update(p)}>Update</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      <Pager page={page} size={PAGE} total={shown.length} onPage={setPage} unit="presets" />
      {canReorder && view === 'list' && shown.length > 1 && (
        <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>Drag rows to set the order of the live view’s preset buttons.</div>
      )}
    </div>
  );
}

function PresetCard({ cameraId, p, at, canPtz, canManage, busy, onGo, onPeek, onDelete, onRename, onHome, onPicture, onRestore, onUpdate }: {
  cameraId: string; p: PtzPreset; at: boolean; canPtz: boolean; canManage: boolean; busy: boolean;
  onGo: () => void; onDelete: () => void; onRename: (name: string) => void; onHome: () => void;
  onPicture: () => void; onRestore: () => void; onPeek?: (p: PtzPreset | null) => void;
  /** set this preset to the current view */
  onUpdate: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(p.name || '');
  const title = p.name || p.token;
  return (
    <div className={`ptz-preset-card${at ? ' at' : ''}${p.missing_on_camera ? ' missing' : ''}`}
         onMouseEnter={() => onPeek?.(p)} onMouseLeave={() => onPeek?.(null)}>
      <button type="button" className="ptz-preset-thumb" disabled={!canPtz || busy || p.missing_on_camera}
              title={p.missing_on_camera ? 'The camera no longer has this preset' : `Go to ${title}`} onClick={onGo}>
        {p.has_thumbnail
          ? <AuthImg path={`/cameras/${cameraId}/ptz/presets/${encodeURIComponent(p.token)}/thumbnail?v=${encodeURIComponent(p.thumbnail_at || '')}`} alt={title} />
          : <span className="ptz-preset-nopic">no picture</span>}
        {at && <span className="ptz-preset-here">📍 here now</span>}
      </button>
      <div className="ptz-preset-meta">
        {editing ? (
          <form onSubmit={e => { e.preventDefault(); if (name.trim()) { onRename(name.trim()); setEditing(false); } }}
                style={{ display: 'flex', gap: 4 }}>
            <input value={name} onChange={e => setName(e.target.value)} maxLength={64} autoFocus style={{ width: '100%' }} />
            <button className="btn-primary btn-sm" type="submit" disabled={!name.trim()}>OK</button>
          </form>
        ) : (
          <strong style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={title}>
            {p.is_home ? '⌂ ' : ''}{title}
          </strong>
        )}
        {p.missing_on_camera && <div style={{ fontSize: 11, color: 'var(--warn, #f59e0b)' }}>Missing on the camera</div>}
        {canManage && !editing && (
          <div className="ptz-preset-actions">
            <button type="button" className="btn-subtle btn-xs" disabled={busy} onClick={() => setEditing(true)}>Rename</button>
            <button type="button" className="btn-subtle btn-xs" disabled={busy} onClick={onHome}
                    title="Home = where an idle camera returns">{p.is_home ? 'Unset home' : 'Make home'}</button>
            {p.missing_on_camera
              ? <button type="button" className="btn-subtle btn-xs" disabled={busy} onClick={onRestore}
                        title="Moves the camera to the stored position and saves it on the camera again">Restore</button>
              : <button type="button" className="btn-subtle btn-xs" disabled={busy || !at} onClick={onPicture}
                        title={at ? 'Take the picture again' : 'Go to this preset first'}>Picture</button>}
            {!p.missing_on_camera && (
              <button type="button" className="btn-subtle btn-xs" disabled={busy || at} onClick={onUpdate}
                      title={at ? 'The camera is already here' : 'Set this preset to where the camera is pointing now'}>Update</button>
            )}
            <button type="button" className="btn-subtle btn-xs" disabled={busy} onClick={onDelete}>Delete</button>
          </div>
        )}
      </div>
    </div>
  );
}
