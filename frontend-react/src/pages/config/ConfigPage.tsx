/**
 * ConfigPage.tsx — per-camera Configuration page (port of the legacy
 * cfgInit / cfgRender / cfgTab / cfgOpen / cfgLoadThumb). Deep-linkable via
 * ?cam=<cameraId>&tab=<recording|ai|device|health>.
 */
import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { apiBlob, apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { useCameras, zoneOf } from '@/lib/cameras';
import type { Camera } from '@/lib/types';
import { Sparkline } from '@/components/Sparkline';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import { CamerasTabs } from '@/pages/cameras/CamerasPage';
import { EditCameraModal } from '@/pages/cameras/EditCameraModal';
import { Chip, StatusBadge } from './bits';
import { RecordingTab } from './tabs/RecordingTab';
import { AiTab } from './tabs/AiTab';
import { PrivacyTab } from './tabs/PrivacyTab';
import { AnalyticsTab } from './tabs/AnalyticsTab';
import { DeviceTab } from './tabs/DeviceTab';
import { HealthTab } from './tabs/HealthTab';

type TabKey = 'recording' | 'ai' | 'analytics' | 'privacy' | 'device' | 'health';
const TABS: [TabKey, string][] = [
  ['recording', 'Recording'],
  ['ai', 'AI Config'],
  ['analytics', 'Zones & Analytics'],
  ['privacy', 'Privacy'],
  ['device', 'Device'],
  ['health', 'Health'],
];
// Roadmap tabs, rendered disabled with a visible badge rather than a hover-only
// title. Keep this list honest: a tab belongs here only while nothing in the
// product does the job. 'Audit log' used to sit here and was wrong — the
// hash-chained trail has shipped, so the tab now deep links into it (below)
// instead of advertising a feature that already exists somewhere else.
const SOON_TABS = ['PTZ'];
const isTabKey = (t: string | null): t is TabKey => TABS.some(([k]) => k === t);

/** 220×130 snapshot preview — authenticated fetch (apiBlob) → object URL. */
function SnapshotThumb({ camera }: { camera: Camera }) {
  const [url, setUrl] = useState<string | null>(null);
  const live = camera.enabled && camera.health_status === 'connected';

  useEffect(() => {
    setUrl(null);
    if (!live) return;
    let alive = true;
    let obj: string | null = null;
    apiBlob(`/cameras/${camera.id}/snapshot?t=${Date.now()}`)
      .then(blob => {
        if (!alive) return;
        obj = URL.createObjectURL(blob);
        setUrl(obj);
      })
      .catch(() => { /* snapshot unavailable — keep the dark placeholder */ });
    return () => { alive = false; if (obj) URL.revokeObjectURL(obj); };
  }, [camera.id, live]);

  return (
    <div style={{
      position: 'relative', width: 220, height: 130, borderRadius: 9, overflow: 'hidden',
      border: '1px solid var(--border)', background: '#0a0e13', flexShrink: 0,
    }}>
      {url && <img src={url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />}
      <span className="lv-badge" style={{ position: 'absolute', top: 7, right: 7 }}>
        {camera.health_status === 'connected' ? 'LIVE' : 'OFFLINE'}
      </span>
    </div>
  );
}

export function ConfigPage() {
  const nav = useNavigate();
  const toast = useToast();
  const { cameras, motion, uptime, loading, error, refresh } = useCameras();
  // Reading the audit trail is require_role("admin", "dpo") server-side.
  const { me, isAdmin } = useAuth();
  const canAudit = isAdmin || (me?.roles || []).includes('dpo');
  const [params, setParams] = useSearchParams();
  const [editing, setEditing] = useState<Camera | null>(null);
  const [deleting, setDeleting] = useState<Camera | null>(null);
  const [purgeFootage, setPurgeFootage] = useState(false);
  const [typedConfirm, setTypedConfirm] = useState('');
  const [deleteBusy, setDeleteBusy] = useState(false);

  const camParam = params.get('cam');
  const tabParam = params.get('tab');
  const tab: TabKey = isTabKey(tabParam) ? tabParam : 'recording';
  const camera = cameras.find(c => c.id === camParam) ?? cameras[0];

  const setParam = (key: 'cam' | 'tab', value: string) => {
    const next = new URLSearchParams(params);
    next.set(key, value);
    setParams(next, { replace: true });
  };

  async function reconnect(c: Camera) {
    try {
      await apiFetch(`/cameras/${c.id}/reconnect`, { method: 'POST' });
      toast(`Reconnect triggered for ${c.slug}`);
      setTimeout(refresh, 2000);
    } catch (e: any) { toast(e.message, 'err'); }
  }

  async function toggleEnabled(c: Camera) {
    try {
      await apiFetch(`/cameras/${c.id}`, { method: 'PUT', body: JSON.stringify({ enabled: !c.enabled }) });
      toast(!c.enabled ? 'Camera enabled — reconnecting…' : 'Camera disabled (slug & footage kept)');
      setTimeout(refresh, 1000);
    } catch (e: any) { toast(e.message, 'err'); }
  }

  function openDelete(c: Camera) {
    setDeleting(c);
    setPurgeFootage(false);
    setTypedConfirm('');
  }

  async function confirmDelete() {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      const q = purgeFootage ? '?purge_recordings=true' : '';
      await apiFetch(`/cameras/${deleting.id}${q}`, { method: 'DELETE' });
      toast(purgeFootage ? 'Camera and footage deleted' : 'Camera deleted');
      setDeleting(null);
      const next = new URLSearchParams(params);
      next.delete('cam');
      setParams(next, { replace: true });
      refresh();
    } catch (e: any) {
      toast(e.message, 'err');
    } finally {
      setDeleteBusy(false);
    }
  }

  const renderBody = (c: Camera) => (
    <>
      {/* Identity header — snapshot, name/status, vital stats, actions. The
          destructive action is pushed to the far right, away from the rest. */}
      <div className="card">
        <div style={{ display: 'flex', gap: 'var(--s5)', alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <SnapshotThumb camera={c} />
          <div style={{ flex: 1, minWidth: 280 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s3)', flexWrap: 'wrap' }}>
              <span style={{ fontSize: 19, fontWeight: 650, letterSpacing: '-.028em' }}>{c.name}</span>
              <StatusBadge status={c.health_status} enabled={c.enabled} />
            </div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--dim)', margin: '4px 0 var(--s5)' }}>
              {c.slug} · {[c.vendor, c.model].filter(Boolean).join(' ') || 'unknown model'}
            </div>
            <div style={{ display: 'flex', gap: 'var(--s7)', flexWrap: 'wrap' }}>
              <Chip label="Uptime 7d"><Sparkline entry={uptime[c.id]} withPct /></Chip>
              <Chip label="Firmware">{c.firmware || '—'}</Chip>
              <Chip label="Zone">{zoneOf(c) === 'Unzoned' ? '—' : zoneOf(c)}</Chip>
              <Chip label="IP"><span style={{ fontFamily: 'var(--mono)', fontSize: 12.5 }}>{c.ip || '—'}</span></Chip>
            </div>
            <div style={{ display: 'flex', gap: 'var(--s2)', marginTop: 'var(--s5)', flexWrap: 'wrap', alignItems: 'center' }}>
              <button className="btn-ghost btn-sm" onClick={() => nav(`/live?cam=${c.id}`)}>▶ Live</button>
              <button className="btn-ghost btn-sm" onClick={() => setEditing(c)}>Edit</button>
              <button className="btn-ghost btn-sm" onClick={() => reconnect(c)}>Reconnect</button>
              <button className="btn-ghost btn-sm" onClick={() => toggleEnabled(c)}>{c.enabled ? 'Disable' : 'Enable'}</button>
              <span style={{ flex: 1 }} />
              <button className="btn-subtle btn-sm" style={{ color: 'var(--red)' }} onClick={() => openDelete(c)}>Delete camera</button>
            </div>
          </div>
        </div>
      </div>

      {/* Sub-tabs */}
      <div className="tabs page-tabs" style={{ marginBottom: 16 }}>
        {TABS.map(([key, label]) => (
          <div key={key} className={`tab${tab === key ? ' active' : ''}`} onClick={() => setParam('tab', key)}>
            {label}
          </div>
        ))}
        {SOON_TABS.map(label => (
          <div key={label} className="tab disabled" title="PTZ arrives with the ONVIF PTZ service">
            {label}<span className="nav-soon" style={{ marginLeft: 6 }}>soon</span>
          </div>
        ))}
        {/* The audit trail is admin/DPO-only server-side, so only show the tab
            to someone who can actually read it — otherwise it's a link to a 403.
            `q` seeds the log's filter, which matches actor/action/target/detail,
            so the slug pulls this camera's rows. */}
        {canAudit && (
          <div className="tab" title={`Audit trail for ${c.name || c.slug}`}
               onClick={() => nav(`/admin?tab=audit&q=${encodeURIComponent(c.slug)}`)}>
            Audit log
          </div>
        )}
      </div>

      <div className="card">
        {tab === 'recording' && <RecordingTab key={c.id} camera={c} onSaved={refresh} />}
        {tab === 'ai' && <AiTab key={c.id} camera={c} motionState={motion[c.slug]} onSaved={refresh} />}
        {tab === 'analytics' && <AnalyticsTab key={c.id} camera={c} onSaved={refresh} />}
        {tab === 'privacy' && <PrivacyTab key={c.id} camera={c} onSaved={refresh} />}
        {tab === 'device' && <DeviceTab key={c.id} camera={c} onRefresh={refresh} />}
        {tab === 'health' && <HealthTab key={c.id} camera={c} />}
      </div>
    </>
  );

  return (
    <div className="fade">
      {/* Title lives in the topbar (like Live View) — tabs lead the page. */}
      <CamerasTabs active="config" />

      {/* A picker this small doesn't earn a card of its own — it's a toolbar. */}
      <div className="inv-toolbar">
        <span className="label">Camera</span>
        <select value={camera?.id ?? ''} onChange={e => setParam('cam', e.target.value)}
                style={{ width: 'auto', minWidth: 280, maxWidth: 380 }}>
          {cameras.map(c => (
            <option key={c.id} value={c.id}>{c.name} ({c.slug})</option>
          ))}
        </select>
      </div>

      {camera
        ? renderBody(camera)
        : (
          <div className="card">
            <div className="empty">
              {error || (loading ? 'Loading cameras…' : 'No cameras registered yet.')}
            </div>
          </div>
        )}

      <EditCameraModal camera={editing} onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); refresh(); }} />

      <Modal open={!!deleting} title="Delete camera" width={480}
        onClose={() => { if (!deleteBusy) setDeleting(null); }}>
        {deleting && (
          <>
            <div style={{ fontSize: 13, marginBottom: 12 }}>
              Camera <b>{deleting.name}</b> (<span style={{ fontFamily: 'var(--mono)' }}>{deleting.slug}</span>) will be
              permanently removed — the relay stream and recording stop, and re-adding it later creates a new identity
              (new slug/URL/timeline). If this is temporary, use <b>Disable</b> instead — it keeps the slug and timeline.
            </div>
            <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: 13, cursor: 'pointer' }}>
              <input type="checkbox" style={{ width: 'auto', marginTop: 2 }}
                checked={purgeFootage}
                onChange={e => { setPurgeFootage(e.target.checked); setTypedConfirm(''); }} />
              <span>Also permanently delete all recorded footage for this camera</span>
            </label>
            <div className="d-hint" style={{ margin: '4px 0 12px 26px' }}>
              {purgeFootage
                ? 'Every recorded segment is erased from disk immediately. This cannot be undone.'
                : 'Leave unchecked to keep existing footage until its retention expires.'}
            </div>
            {purgeFootage && (
              <div className="form-group full">
                <label>Type DELETE to confirm footage removal</label>
                <input value={typedConfirm} onChange={e => setTypedConfirm(e.target.value)}
                  autoComplete="off" placeholder="DELETE" />
              </div>
            )}
            <div className="form-actions">
              <button className="btn-danger" onClick={confirmDelete}
                disabled={deleteBusy || (purgeFootage && typedConfirm !== 'DELETE')}>
                {deleteBusy ? 'Deleting…' : purgeFootage ? 'Delete camera + footage' : 'Delete camera'}
              </button>
              <button className="btn-ghost" onClick={() => setDeleting(null)} disabled={deleteBusy}>Cancel</button>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}
