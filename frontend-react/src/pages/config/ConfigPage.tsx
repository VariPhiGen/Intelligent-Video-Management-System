/**
 * ConfigPage.tsx — per-camera Configuration page (port of the legacy
 * cfgInit / cfgRender / cfgTab / cfgOpen / cfgLoadThumb). Deep-linkable via
 * ?cam=<cameraId>&tab=<recording|ai|device|health>.
 */
import { ViewTransition } from '@/components/ViewTransition';
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
import { ensurePtzChecked } from '@/lib/ptz';
import { EditCameraModal } from '@/pages/cameras/EditCameraModal';
import { Chip, StatusBadge } from './bits';
import { RecordingTab } from './tabs/RecordingTab';
import { AiTab } from './tabs/AiTab';
import { PrivacyTab } from './tabs/PrivacyTab';
import { AnalyticsTab } from './tabs/AnalyticsTab';
import { PresetViewScope } from './PresetViewScope';
import { PtzTab, ptzShowsLive } from './tabs/PtzTab';
import { DeviceTab } from './tabs/DeviceTab';
import { HealthTab } from './tabs/HealthTab';
import { TimelapseTab } from './tabs/TimelapseTab';

type TabKey = 'recording' | 'ai' | 'analytics' | 'privacy' | 'timelapse' | 'ptz' | 'device' | 'health';
const TABS: [TabKey, string][] = [
  ['recording', 'Recording'],
  ['ai', 'AI Config'],
  ['analytics', 'Zones & Analytics'],
  ['privacy', 'Privacy'],
  ['timelapse', 'Time-lapse'],
  ['ptz', 'PTZ'],
  ['device', 'Device'],
  ['health', 'Health'],
];
// PTZ is the one tab that is not universal: it appears only on cameras the
// DEVICE reported as movable (Camera.ptz_capable, probed over ONVIF — see
// services/onvif_ptz.py). A greyed "PTZ" tab on every fixed camera in the
// fleet would be noise on the overwhelming majority of them, and a disabled
// tab that can never become enabled is not information.
//
// There is deliberately no SOON_TABS list any more. It held exactly one entry,
// 'PTZ', and the tab it advertised now exists. The rule that governed it still
// stands for whatever comes next: a tab may only claim "soon" while nothing in
// the product does the job — 'Audit log' sat here once and was already wrong,
// because the hash-chained trail had shipped and the tab now deep links to it.
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
  // Time-lapse is its own grant (timelapse_view): a tab its API would 403 is a dead end.
  const canTimelapse = isAdmin || me?.permissions?.timelapse_view === true;
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

  // Self-healing PTZ check: opening a camera's config is "first use" for a
  // camera nobody has asked about PTZ yet (discovery-onboarded ones store
  // credentials without ever hitting the endpoint that probes). One SOAP
  // round trip per camera ever — GET /ptz probes on a NULL cache and
  // persists — then refresh() lets ptz_capable surface the PTZ tab.
  useEffect(() => {
    if (!camera || !camera.onvif_capable || camera.ptz !== null) return;
    let alive = true;
    ensurePtzChecked(camera).then(caps => { if (alive && caps) refresh(); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera?.id, camera?.ptz]);

  const setParam = (key: 'cam' | 'tab' | 'view', value: string) => {
    const next = new URLSearchParams(params);
    next.set(key, value);
    if (key === 'cam') next.delete('view');            // presets belong to one camera
    setParams(next, { replace: true });
  };
  // PTZ: which view the Zones / AI tabs edit — "camera" or a preset token (PresetViewScope).
  const view = params.get('view') || 'camera';

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

  // The PTZ tab's big live picture replaces the header's small still (one picture on screen, never two), and the
  // header drops to one line so the sub-tabs and the picture start higher.
  const bigLive = (c: Camera) => tab === 'ptz' && !!c.ptz_capable && ptzShowsLive(params);

  const renderBody = (c: Camera) => (
    <>
      {bigLive(c) ? (
        <div className="card" style={{ padding: '10px 14px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s3)', flexWrap: 'wrap' }}>
            <span style={{ fontSize: 16, fontWeight: 650, letterSpacing: '-.02em' }}>{c.name}</span>
            <StatusBadge status={c.health_status} enabled={c.enabled} />
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--dim)' }}>
              {c.slug} · {[c.vendor, c.model].filter(Boolean).join(' ') || 'unknown model'}
            </span>
            <span style={{ flex: 1 }} />
            <button className="btn-ghost btn-sm" onClick={() => nav(`/live?cam=${c.id}`)}>▶ Live</button>
            <button className="btn-ghost btn-sm" onClick={() => setEditing(c)}>Edit</button>
            <button className="btn-ghost btn-sm" onClick={() => reconnect(c)}>Reconnect</button>
          </div>
        </div>
      ) : (
      /* Identity header — snapshot, name/status, vital stats, actions. The
          destructive action is pushed to the far right, away from the rest. */
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
      )}

      {/* Sub-tabs */}
      <div className="tabs page-tabs" style={{ marginBottom: 16 }}>
        {TABS.filter(([key]) => (key !== 'ptz' || c.ptz_capable) && (key !== 'timelapse' || canTimelapse)).map(([key, label]) => (
          <button type="button" key={key} aria-pressed={tab === key} className={`tab${tab === key ? ' active' : ''}`} onClick={() => setParam('tab', key)}>
            {label}
          </button>
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
        {tab === 'ai' && (
          <PresetViewScope camera={c} view={view} onView={v => setParam('view', v)} onSaved={refresh}>
            {(vc, target, k, saved) => <AiTab key={`${c.id}:${k}`} camera={vc} target={target}
                                              motionState={motion[c.slug]} onSaved={saved} />}
          </PresetViewScope>
        )}
        {tab === 'analytics' && (
          <PresetViewScope camera={c} view={view} onView={v => setParam('view', v)} onSaved={refresh}>
            {(vc, target, k, saved) => <AnalyticsTab key={`${c.id}:${k}`} camera={vc} target={target} onSaved={saved} />}
          </PresetViewScope>
        )}
        {tab === 'privacy' && <PrivacyTab key={c.id} camera={c} onSaved={refresh} />}
        {tab === 'timelapse' && (canTimelapse
          ? <TimelapseTab key={c.id} camera={c} onSaved={refresh} />
          : <div className="empty">Seeing time-lapse settings needs the “Time-lapse” permission.</div>)}
        {/* ?tab=ptz is deep-linkable and a link can outlive the camera it was
            made for (swapped hardware, a re-probe that came back fixed), so the
            body answers for itself rather than trusting the tab strip. */}
        {tab === 'ptz' && (c.ptz_capable
          ? <PtzTab key={c.id} camera={c} onRefresh={refresh} />
          : <div className="empty">
              {c.ptz?.reason || (c.onvif_capable
                ? 'Checking PTZ support on this camera — the tab appears by itself if it pans.'
                : 'This camera has no stored ONVIF credentials, so PTZ support is unknown — add them under Device → Maintenance.')}
            </div>)}
        {tab === 'device' && <DeviceTab key={c.id} camera={c} onRefresh={refresh} />}
        {tab === 'health' && <HealthTab key={c.id} camera={c} />}
      </div>
    </>
  );

  return (
    <ViewTransition view={`${camera?.id ?? ''}:${tab}`}>
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
    </ViewTransition>
  );
}
