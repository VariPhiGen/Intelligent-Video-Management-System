/**
 * CamerasPage.tsx — the Inventory tab: zone-grouped table with online counts,
 * uptime sparklines, bulk action bar, row-click → the camera's Configuration
 * page. Pattern-setter for the React migration.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { CameraFilterBar } from '@/components/CameraFilterBar';
import { applyFilter, isEmpty, loadFilter, saveFilter, type CameraFilter } from '@/lib/cameraFilters';
import { Link, useNavigate } from 'react-router-dom';
import { apiDownload, apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { useCameras, zoneOf } from '@/lib/cameras';
import { dtLocal } from '@/lib/format';
import type { Camera } from '@/lib/types';
import { Sparkline } from '@/components/Sparkline';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import { EditCameraModal } from './EditCameraModal';
import { BulkApplyModal, DeleteCamerasModal } from './cameraModals';
import { TimelapseBulkModal, type BulkTimelapseMode } from './TimelapseBulkModal';

const STATUS_META: Record<string, [string, string]> = {
  connected: ['var(--green)', 'Online'],
  disconnected: ['var(--red)', 'Offline'],
  error: ['var(--red)', 'Error'],
  unknown: ['var(--yellow)', 'Unknown'],
};

function StatusCell({ c, motion }: { c: Camera; motion?: string }) {
  const [dot, label] = !c.enabled ? ['var(--dim)', 'Disabled'] : (STATUS_META[c.health_status] || ['var(--dim)', c.health_status]);
  const live = c.enabled && c.health_status === 'connected';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 4 }}>
      <span className="dotlabel">
        {/* A live camera's dot breathes; everything else holds still, so motion
            on this screen only ever means "streaming right now". */}
        <span className="d" style={live ? { background: dot, animation: 'pulse 2s var(--ease) infinite', color: dot } : { background: dot }} />
        {label}
      </span>
      {/* Exceptions only — a camera in its normal state adds no badges. */}
      {c.recording === false && <span className="badge badge-yellow">Rec off</span>}
      {motion === 'TRIGGERED' && <span className="badge badge-red badge-live"><span className="badge-dot" />Motion</span>}
      {/* Time-lapse is OFF by default (migration 057), so "on" is an exception
          worth a badge — and it is how a bulk switch shows up row by row. */}
      {c.timelapse_enabled && <span className="badge badge-cyan" title="Time-lapse is capturing stills for this camera">◷ Time-lapse</span>}
    </div>
  );
}

/** A checkbox that can show "some" — the zone header's state when only part of
 *  the zone is ticked. `indeterminate` is a DOM property, not an attribute. */
function TriCheckbox({ checked, some, onChange, label }: {
  checked: boolean; some: boolean; onChange: (on: boolean) => void; label: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = some && !checked; }, [some, checked]);
  return <input ref={ref} type="checkbox" style={{ width: 'auto' }} checked={checked} aria-label={label}
                onClick={e => e.stopPropagation()} onChange={e => onChange(e.target.checked)} />;
}

export function CamerasTabs({ active }: { active: 'inventory' | 'add' | 'config' | 'ptz' }) {
  const { me } = useAuth();
  // Onboarding is gated on camera_manage server-side — hide the tab for a role
  // that can view the inventory but can't add cameras, rather than offer a
  // wizard that 403s on submit.
  const canManage = !!me?.permissions?.camera_manage;
  return (
    <div className="tabs page-tabs">
      <Link className={`tab${active === 'inventory' ? ' active' : ''}`} to="/cameras">Inventory</Link>
      {canManage && <Link className={`tab${active === 'add' ? ' active' : ''}`} to="/cameras/add">Add cameras</Link>}
      <Link className={`tab${active === 'config' ? ' active' : ''}`} to="/cameras/config">Configuration</Link>
      <Link className={`tab${active === 'ptz' ? ' active' : ''}`} to="/cameras/ptz">PTZ</Link>
    </div>
  );
}

export function CamerasPage() {
  const nav = useNavigate();
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  // Camera writes (add / edit / bulk state) are gated on camera_manage; delete
  // stays admin-only. Affordances follow the grant so nobody is shown a button
  // that 403s. Admins hold every capability, so canManage is true for them too.
  const canManage = !!me?.permissions?.camera_manage;
  // Time-lapse has its own grant (routers/timelapse.py explains why), so a site
  // lead without camera_manage can still switch a whole site's time-lapse on.
  const canTimelapse = isAdmin || !!me?.permissions?.timelapse_manage;
  const [tlMode, setTlMode] = useState<BulkTimelapseMode>(null);
  const { cameras, motion, uptime, error, refresh } = useCameras();
  const [q, setQ] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  // Filter chips (PTZ / maker / recording / no ONVIF login); status stays in the dropdown above. Remembered here.
  const [cf, setCfState] = useState<CameraFilter>(() => loadFilter('cameras'));
  const setCf = (f: CameraFilter) => { setCfState(f); saveFilter('cameras', f); };
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<Camera | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [purgeFootage, setPurgeFootage] = useState(false);
  const [typedConfirm, setTypedConfirm] = useState('');
  const [deleteBusy, setDeleteBusy] = useState(false);
  // Confirmation for the "turn off" bulk actions (turning ON stays direct).
  const [pendingBulk, setPendingBulk] = useState<null | 'stopRec' | 'disable'>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  const visible = useMemo(() => {
    const ql = q.toLowerCase();
    return applyFilter(cameras, cf).filter(c => {
      const matchQ = !ql || c.name.toLowerCase().includes(ql) || c.slug.includes(ql)
        || (c.ip || '').includes(ql) || zoneOf(c).toLowerCase().includes(ql);
      return matchQ && (!statusFilter || c.health_status === statusFilter);
    });
  }, [cameras, q, statusFilter, cf]);

  const groups = useMemo(() => {
    const g: Record<string, Camera[]> = {};
    visible.forEach(c => { (g[zoneOf(c)] = g[zoneOf(c)] || []).push(c); });
    return g;
  }, [visible]);

  const online = cameras.filter(c => c.health_status === 'connected').length;
  const offline = cameras.length - online;
  const filtering = !!(q || statusFilter || !isEmpty(cf));
  const sel = [...selected].filter(id => cameras.some(c => c.id === id));

  const toggleSel = (id: string, on: boolean) =>
    setSelected(s => { const n = new Set(s); on ? n.add(id) : n.delete(id); return n; });
  // Tick / untick every camera of one zone (a site), leaving other zones' ticks alone.
  const setZoneSel = (cams: Camera[], on: boolean) =>
    setSelected(s => { const n = new Set(s); cams.forEach(c => (on ? n.add(c.id) : n.delete(c.id))); return n; });
  const toggleZone = (z: string) =>
    setCollapsed(s => { const n = new Set(s); n.has(z) ? n.delete(z) : n.add(z); return n; });

  async function bulkAssignZone() {
    const zone = prompt('Zone / group for the selected cameras (blank to clear):');
    if (zone === null) return;
    for (const id of sel) {
      const c = cameras.find(x => x.id === id); if (!c) continue;
      const meta = { ...(c.metadata || {}) };
      if (zone) meta.zone = zone; else delete meta.zone;
      try { await apiFetch(`/cameras/${id}`, { method: 'PUT', body: JSON.stringify({ metadata: meta }) }); }
      catch (e: any) { toast(`${c.name}: ${e.message}`, 'err'); }
    }
    toast(zone ? `Zone "${zone}" assigned to ${sel.length} camera(s)` : 'Zone cleared');
    refresh();
  }
  async function bulkReconnect() {
    for (const id of sel) {
      try { await apiFetch(`/cameras/${id}/reconnect`, { method: 'POST' }); } catch { /* best-effort */ }
    }
    toast(`Reconnect triggered for ${sel.length} camera(s)`);
    setTimeout(refresh, 2000);
  }
  // Bulk recording toggle. Continuous = recording on with NO schedule, so clear
  // any weekly schedule (same definition the Recording tab uses); off just stops.
  async function bulkSetRecording(on: boolean) {
    let ok = 0;
    const failed: string[] = [];
    for (const id of sel) {
      const c = cameras.find(x => x.id === id);
      try {
        await apiFetch(`/cameras/${id}`, { method: 'PUT', body: JSON.stringify({ recording: on }) });
        if (on) {
          await apiFetch(`/cameras/${id}/schedule`, { method: 'PUT', body: JSON.stringify({ schedule: null }) });
        }
        ok += 1;
      } catch (e: any) {
        failed.push(c?.name ?? id);
      }
    }
    const what = on ? 'Continuous recording on' : 'Recording stopped';
    if (failed.length) toast(`${what} for ${ok}/${sel.length} — failed: ${failed.join(', ')}`, 'err');
    else toast(`${what} for ${ok} camera${ok === 1 ? '' : 's'}`);
    setTimeout(refresh, 800);
  }
  // Enable/disable the whole camera. Disable stops the live stream AND recording
  // (footage is kept); enable brings it back. Distinct from recording on/off.
  async function bulkSetEnabled(on: boolean) {
    let ok = 0;
    const failed: string[] = [];
    for (const id of sel) {
      const c = cameras.find(x => x.id === id);
      try {
        await apiFetch(`/cameras/${id}`, { method: 'PUT', body: JSON.stringify({ enabled: on }) });
        ok += 1;
      } catch (e: any) {
        failed.push(c?.name ?? id);
      }
    }
    const what = on ? 'Enabled' : 'Disabled';
    if (failed.length) toast(`${what} ${ok}/${sel.length} — failed: ${failed.join(', ')}`, 'err');
    else toast(`${what} ${ok} camera${ok === 1 ? '' : 's'}`);
    setTimeout(refresh, 800);
  }
  async function runPendingBulk() {
    if (!pendingBulk) return;
    setBulkBusy(true);
    if (pendingBulk === 'stopRec') await bulkSetRecording(false);
    else await bulkSetEnabled(false);
    setBulkBusy(false);
    setPendingBulk(null);
  }
  function bulkExportCsv() {
    const rows = [['name', 'slug', 'ip', 'zone', 'status', 'uptime_7d_pct', 'firmware', 'local_rtsp_url']];
    for (const id of sel) {
      const c = cameras.find(x => x.id === id); if (!c) continue;
      rows.push([c.name, c.slug, c.ip || '', zoneOf(c) === 'Unzoned' ? '' : zoneOf(c), c.health_status,
        String(uptime[c.id]?.pct ?? ''), c.firmware || '', c.local_rtsp_url]);
    }
    const csv = rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = `cameras_${dtLocal(new Date()).replace(/[:T]/g, '-')}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
  }
  // Admin Excel export (server-built: name + camera ID first, every field incl. MAC/vendor/
  // ports/retention/schedule, a Summary sheet, and never a credential). `ids` = the ticked rows;
  // none ticked = the whole fleet. apiDownload keeps the server's timestamped filename.
  const [exporting, setExporting] = useState(false);
  async function exportXlsx(ids: string[] = []) {
    setExporting(true);
    try {
      const qs = ids.length ? `?ids=${encodeURIComponent(ids.join(','))}` : '';
      await apiDownload(`/cameras/export.xlsx${qs}`, `cameras_${dtLocal(new Date()).replace(/[:T]/g, '-')}.xlsx`);
      const n = ids.length || cameras.length;
      toast(`Exported ${n} camera${n === 1 ? '' : 's'} to Excel`);
    } catch (e: any) {
      toast(e?.message || 'Export failed', 'err');
    } finally { setExporting(false); }
  }
  function openBulkDelete() {
    setConfirmingDelete(true);
    setPurgeFootage(false);
    setTypedConfirm('');
  }
  async function confirmBulkDelete() {
    setDeleteBusy(true);
    const query = purgeFootage ? '?purge_recordings=true' : '';
    for (const id of sel) {
      try { await apiFetch(`/cameras/${id}${query}`, { method: 'DELETE' }); } catch (e: any) { toast(e.message, 'err'); }
    }
    setDeleteBusy(false);
    setConfirmingDelete(false);
    toast(purgeFootage ? 'Cameras and footage deleted' : `${sel.length} camera(s) deleted`);
    setSelected(new Set());
    refresh();
  }

  const allChecked = visible.length > 0 && sel.length === visible.length;

  return (
    <div className="fade">
      {/* Title lives in the topbar (like Live View) — tabs lead the page. */}
      <CamerasTabs active="inventory" />

      {/* Toolbar: query controls left, the one primary action right. Fleet
          counts moved onto the table's own header, where the data is. */}
      <div className="inv-toolbar">
        <div className="inv-search">
          <span className="ico">⌕</span>
          <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search cameras, IP, zone…" />
        </div>
        <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} style={{ maxWidth: 158, width: 'auto' }}>
          <option value="">All statuses</option>
          <option value="connected">Online</option>
          <option value="disconnected">Offline</option>
          <option value="unknown">Unknown</option>
          <option value="error">Error</option>
        </select>
        {filtering && (
          <button className="btn-subtle btn-sm" onClick={() => { setQ(''); setStatusFilter(''); setCf({}); }}>Clear filters</button>
        )}
        <span style={{ flex: 1 }} />
        {canManage && <button className="btn-primary" onClick={() => nav('/cameras/add')}>＋ Add camera</button>}
      </div>
      <CameraFilterBar cameras={cameras} value={cf} onChange={setCf} groups={['type', 'maker', 'recording', 'attention']} />

      {/* Bulk action bar. The state-changing actions need camera_manage; Export is a read so it
          stays open — as the server-built Excel workbook for admins, the client-side CSV of what
          the table shows for everyone else; Delete stays admin-only. */}
      {sel.length > 0 && (
        <div className="inv-bulk">
          <span className="sel">{sel.length} selected</span>
          {canManage && (
            <>
              <span className="inv-sep" />
              <button className="btn-ghost btn-sm" onClick={bulkAssignZone}>Assign zone</button>
              <button className="btn-ghost btn-sm" onClick={bulkReconnect}>Restart</button>
              <span className="inv-sep" />
              <button className="btn-ghost btn-sm" onClick={() => bulkSetEnabled(true)}>Enable</button>
              <button className="btn-ghost btn-sm" onClick={() => setPendingBulk('disable')}>Disable</button>
              <span className="inv-sep" />
              <button className="btn-ghost btn-sm" onClick={() => bulkSetRecording(true)}>Continuous rec</button>
              <button className="btn-ghost btn-sm" onClick={() => setPendingBulk('stopRec')}>Stop rec</button>
            </>
          )}
          {canTimelapse && (
            <>
              <span className="inv-sep" />
              <button className="btn-ghost btn-sm" onClick={() => setTlMode('on')}
                      title="Switch time-lapse on for the selected cameras">◷ Time-lapse on</button>
              <button className="btn-ghost btn-sm" onClick={() => setTlMode('off')}>Time-lapse off</button>
            </>
          )}
          <span className="inv-sep" />
          <button className="btn-ghost btn-sm" disabled={exporting}
                  onClick={() => (isAdmin ? exportXlsx(sel) : bulkExportCsv())}>
            {isAdmin ? (exporting ? 'Exporting…' : 'Export to Excel') : 'Export'}
          </button>
          {isAdmin && <button className="btn-danger btn-sm" onClick={openBulkDelete}>Delete</button>}
          <span style={{ flex: 1 }} />
          <button className="btn-ghost btn-sm" onClick={() => setSelected(new Set())}>Clear</button>
        </div>
      )}

      <div className="panel">
        <div className="panel-head">
          <div className="panel-head-l">
            <div>
              <div className="panel-title">Fleet</div>
              <div className="panel-sub">
                {filtering ? `${visible.length} of ${cameras.length} shown` : 'Grouped by zone · select a row to configure'}
              </div>
            </div>
          </div>
          <div className="panel-actions">
            <div className="inv-statline">
              <span className="s"><b>{cameras.length}</b> cameras</span>
              <span className="s"><span className="d" style={{ background: 'var(--green)' }} /><b>{online}</b> online</span>
              <span className="s"><span className="d" style={{ background: offline ? 'var(--red)' : 'var(--dim)' }} /><b>{offline}</b> offline</span>
            </div>
            {isAdmin && cameras.length > 0 && (
              <button className="btn-ghost btn-sm" disabled={exporting} onClick={() => exportXlsx(sel)}
                      title={sel.length ? `Export the ${sel.length} selected cameras to Excel`
                                        : 'Export every camera (name, camera ID, IP, MAC, model, ports, retention, status…) to Excel'}>
                {exporting ? 'Exporting…' : `⤓ Export to Excel${sel.length ? ` (${sel.length})` : ''}`}
              </button>
            )}
          </div>
        </div>
        <div className="panel-body flush">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 34 }}>
                  <input type="checkbox" style={{ width: 'auto' }} checked={allChecked}
                    aria-label="Select all cameras shown"
                    onChange={e => setSelected(e.target.checked ? new Set(visible.map(c => c.id)) : new Set())} />
                </th>
                <th>Camera</th><th>IP address</th><th>Zone</th><th>Status</th><th>Uptime 7d</th><th>Firmware</th>
              </tr>
            </thead>
            <tbody>
              {error && <tr><td colSpan={7}><div className="empty">{error}</div></td></tr>}
              {!error && !visible.length && (
                <tr><td colSpan={7} style={{ padding: 0 }}>
                  <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                    <div className="glyph">{filtering ? '⌕' : '▤'}</div>
                    <h4>{filtering ? 'No cameras match' : 'No cameras yet'}</h4>
                    <p>{filtering
                      ? 'Nothing matches this search and these filters.'
                      : 'Add your first camera to start monitoring, recording and analytics.'}</p>
                    {filtering
                      ? <button className="btn-ghost btn-sm" onClick={() => { setQ(''); setStatusFilter(''); }}>Clear filters</button>
                      : canManage && <button className="btn-primary btn-sm" onClick={() => nav('/cameras/add')}>＋ Add camera</button>}
                  </div>
                </td></tr>
              )}
              {Object.keys(groups).sort().map(z => {
                const cams = groups[z];
                const zOnline = cams.filter(c => c.health_status === 'connected').length;
                const isCollapsed = collapsed.has(z);
                const zSel = cams.filter(c => selected.has(c.id)).length;
                return [
                  <tr key={`z-${z}`} className="inv-zone" onClick={() => toggleZone(z)}>
                    <td colSpan={7}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', userSelect: 'none' }}>
                        <TriCheckbox checked={zSel === cams.length} some={zSel > 0}
                          label={`Select all cameras in ${z}`} onChange={on => setZoneSel(cams, on)} />
                        <span style={{ fontSize: 10, color: 'var(--dim)', width: 10, display: 'inline-block',
                                       transition: 'transform .15s ease', transform: isCollapsed ? 'none' : 'rotate(90deg)' }}>▶</span>
                        <span style={{ width: 7, height: 7, borderRadius: '50%', background: zOnline ? 'var(--green)' : 'var(--dim)' }} />
                        <span style={{ fontSize: 12, fontWeight: 650, color: 'var(--accent2)', letterSpacing: '.01em' }}>{z}</span>
                        <span style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--muted)' }}>
                          {cams.length} camera{cams.length !== 1 ? 's' : ''} · {zOnline} online
                        </span>
                      </div>
                    </td>
                  </tr>,
                  ...(isCollapsed ? [] : cams.map(c => (
                    <tr key={c.id} className="inv-row" title="Open configuration"
                        onClick={() => nav(`/cameras/config?cam=${c.id}`)}>
                      <td onClick={e => e.stopPropagation()}>
                        <input type="checkbox" style={{ width: 'auto' }} checked={selected.has(c.id)}
                          aria-label={`Select ${c.name}`}
                          onChange={e => toggleSel(c.id, e.target.checked)} />
                      </td>
                      <td>
                        <div style={{ fontWeight: 600 }}>{c.name}</div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2 }} onClick={e => e.stopPropagation()}>
                          {/* Long vendor/model strings ellipsize — wrapping one
                              doubles the row height and breaks the grid. */}
                          <span style={{ color: 'var(--dim)', fontSize: 10.5, fontFamily: 'var(--mono)',
                                         whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                                         maxWidth: 200 }}
                                title={[c.vendor, c.model].filter(Boolean).join(' ') || c.slug}>
                            {[c.vendor, c.model].filter(Boolean).join(' ') || c.slug}
                          </span>
                          {/* Row-level utilities stay chrome-less until hover —
                              they'd otherwise compete with the camera name. */}
                          <button className="iconbtn sm"
                            title={`Copy relay RTSP URL — ${c.local_rtsp_url}`}
                            onClick={() => { navigator.clipboard.writeText(c.local_rtsp_url); toast('RTSP URL copied'); }}>⧉</button>
                          {canManage && <button className="btn-subtle btn-sm" style={{ fontSize: 11 }}
                            onClick={() => setEditing(c)}>Edit</button>}
                        </div>
                      </td>
                      <td style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--text2)' }}>{c.ip || '—'}</td>
                      <td>{zoneOf(c) !== 'Unzoned'
                        ? <span className="badge badge-gray">{zoneOf(c)}</span>
                        : <span style={{ color: 'var(--dim)' }}>—</span>}</td>
                      <td><StatusCell c={c} motion={motion[c.slug]} /></td>
                      <td><Sparkline entry={uptime[c.id]} /></td>
                      <td style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>{c.firmware || '—'}</td>
                    </tr>
                  ))),
                ];
              })}
            </tbody>
          </table>
        </div>
        </div>
      </div>

      <EditCameraModal camera={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); refresh(); }} />

      <DeleteCamerasModal {...{
        confirmingDelete, setConfirmingDelete, deleteBusy, sel, purgeFootage,
        setPurgeFootage, typedConfirm, setTypedConfirm, confirmBulkDelete,
      }} />
      <TimelapseBulkModal mode={tlMode} ids={sel} onClose={() => setTlMode(null)} onDone={refresh} />
      <BulkApplyModal {...{
        pendingBulk, setPendingBulk, bulkBusy, runPendingBulk, sel,
      }} />
    </div>
  );
}
