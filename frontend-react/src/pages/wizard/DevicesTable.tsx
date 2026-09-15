/**
 * DevicesTable.tsx — the discovered-device table in the wizard's assign step,
 * with the two cells that need their own logic: reachability status and the
 * ONVIF profile summary.
 */
import { useState } from 'react';

import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import { CredsModal } from './StepCredentials';
import {
  DSC_META, bestProfile, dscIsCamera, wzMeta,
  type Device,
} from './useDiscovery';

export function DevicesTable({ devices, loadDevices, onSetCreds, onOpenAdd }: {
  devices: Device[];
  loadDevices: () => Promise<void>;
  onSetCreds: (d: Device) => void;
  onOpenAdd: (d: Device) => void;
}) {
  const toast = useToast();
  const [filter, setFilter] = useState('');
  const [showAll, setShowAll] = useState(false);

  const visible = showAll ? devices : devices.filter(dscIsCamera);
  const rows = visible.filter(d => !filter || d.status === filter);
  const hidden = devices.length - devices.filter(dscIsCamera).length;
  const counts: Record<string, number> = {};
  visible.forEach(d => { counts[d.status] = (counts[d.status] || 0) + 1; });
  const order = ['verified', 'auth_failed', 'discovered', 'no_onvif', 'added', 'ignored', 'unreachable'];

  async function verify(id: string) {
    try {
      await apiFetch(`/discovery/devices/${id}/verify`, { method: 'POST' });
      toast('Re-verified', 'ok');
      loadDevices();
    } catch (e: any) { toast(e.message, 'err'); }
  }
  async function ignore(id: string) {
    try {
      await apiFetch(`/discovery/devices/${id}/ignore`, { method: 'POST' });
      loadDevices();
    } catch (e: any) { toast(e.message, 'err'); }
  }
  async function remove(id: string) {
    if (!confirm('Remove this device from the discovery list?')) return;
    try {
      await apiFetch(`/discovery/devices/${id}`, { method: 'DELETE' });
      loadDevices();
    } catch (e: any) { toast(e.message, 'err'); }
  }

  return (
    <div>
      <div className="chips">
        <div className={`chip${filter === '' ? ' active' : ''}`} onClick={() => setFilter('')}>
          <b>{visible.length}</b> {showAll ? 'All devices' : 'Cameras'}
        </div>
        {order.filter(s => counts[s]).map(s => {
          const [, color, label] = DSC_META[s] || ['', 'var(--muted)', s];
          return (
            <div key={s} className={`chip${filter === s ? ' active' : ''}`} onClick={() => setFilter(s)}>
              <span className="cdot" style={{ background: color }} />
              <b>{counts[s]}</b> {label}
            </div>
          );
        })}
        {hidden > 0 && (
          <div className={`chip${showAll ? ' active' : ''}`} onClick={() => setShowAll(v => !v)}
            title="Devices that answered the scan but show no camera evidence (no RTSP/ONVIF)">
            {showAll ? 'Hide' : 'Show'} <b>{hidden}</b> non-camera
          </div>
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
        <div className="card-title" style={{ margin: 0 }}>Discovered devices</div>
        <button className="btn-ghost btn-sm" onClick={() => loadDevices()}>↻ Refresh</button>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>IP</th><th>Vendor / Model</th><th>Status</th><th>Open ports</th>
              <th>RTSP profiles</th><th>Creds</th><th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {!rows.length && (
              <tr><td colSpan={7} className="empty">
                No cameras{filter ? ` with status ${filter}` : ''} found
                {devices.length ? ' — use the "non-camera" chip to see other scanned devices' : ''}.
              </td></tr>
            )}
            {rows.map(d => (
              <tr key={d.id}>
                <td>
                  <code>{d.ip || '—'}</code>
                  {d.error && <div style={{ color: 'var(--muted)', fontSize: 10 }}>{d.error}</div>}
                </td>
                <td>
                  {d.vendor || '—'}
                  {d.model && <span style={{ color: 'var(--muted)' }}> {d.model}</span>}
                </td>
                <td><StatusBadge status={d.status} /></td>
                <td><code style={{ color: 'var(--muted)' }}>{(d.open_ports || []).join(', ') || '—'}</code></td>
                <td><ProfilesCell d={d} /></td>
                <td>
                  {d.has_credentials
                    ? <code>{d.username || 'set'}</code>
                    : <span style={{ color: 'var(--muted)' }}>—</span>}
                </td>
                <td>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {['discovered', 'auth_failed', 'no_onvif'].includes(d.status) && (
                      <button className="btn-ghost btn-sm" onClick={() => onSetCreds(d)}>Set creds</button>
                    )}
                    {d.status === 'verified' && (
                      <>
                        <button className="btn-ok btn-sm" onClick={() => onOpenAdd(d)}>Add</button>
                        <button className="btn-ghost btn-sm" onClick={() => verify(d.id)}>Re-verify</button>
                        <button className="btn-ghost btn-sm" onClick={() => onSetCreds(d)}>Creds</button>
                      </>
                    )}
                    {d.status !== 'ignored' && d.status !== 'added' && (
                      <button className="btn-ghost btn-sm" onClick={() => ignore(d.id)}>Ignore</button>
                    )}
                    <button className="btn-danger btn-sm" title="Remove from list" onClick={() => remove(d.id)}>✕</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const [cls, , label] = DSC_META[status] || ['badge-gray', '', status];
  return <span className={`badge ${cls}`}><span className="badge-dot" />{label || status}</span>;
}

export function ProfilesCell({ d }: { d: Device }) {
  if (!d.rtsp_candidates || !d.rtsp_candidates.length) return <span style={{ color: 'var(--muted)' }}>—</span>;
  return (
    <>
      {d.rtsp_candidates.map((c, i) => (
        <div key={i} style={{ whiteSpace: 'nowrap' }}>
          {c.verified === true && <span className="stream-ok" title="stream verified">● </span>}
          {c.verified === false && <span className="stream-no" title="stream not confirmed">○ </span>}
          <code>{c.profile || c.token}</code>
        </div>
      ))}
    </>
  );
}

// ── Per-device "Add to RTSP relay" modal (legacy dscOpenAdd/dscSubmitAdd) ────
