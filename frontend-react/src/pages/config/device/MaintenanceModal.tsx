/**
 * MaintenanceModal.tsx — ONVIF credentials, clock sync and reboot, ported from
 * the legacy csRenderMaint / csSaveCreds / csSyncTime / csReboot.
 *
 * This is the tab the other three send you to: without working credentials the
 * encoder/imaging/OSD editors have nothing to talk to. Credentials are verified
 * against the camera BEFORE they are stored, so a save that succeeds means the
 * camera really answered.
 */
import { useState } from 'react';
import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import type { Camera } from '@/lib/types';
import { Label } from './onvif';

interface VerifyResp {
  status: string;
  vendor: string | null;
  model: string | null;
  firmware: string | null;
  onvif_port: number;
}
interface SyncResp {
  skew_before_seconds: number | null;
  synced_to: string;
}

export function MaintenanceModal({ camera, onClose, onChanged }: {
  camera: Camera;
  onClose: () => void;
  /** Credentials changed — the camera's onvif_capable flips, so refresh it. */
  onChanged: () => void;
}) {
  const toast = useToast();
  const [user, setUser] = useState('admin');
  const [pass, setPass] = useState('');
  const [port, setPort] = useState(String(camera.onvif_port || 80));
  const [credBusy, setCredBusy] = useState(false);
  const [credOut, setCredOut] = useState('');

  const [syncBusy, setSyncBusy] = useState(false);
  const [syncOut, setSyncOut] = useState('');

  const [rebooted, setRebooted] = useState(false);
  const [rebootBusy, setRebootBusy] = useState(false);

  const [err, setErr] = useState('');

  async function saveCreds() {
    if (!user.trim()) { setErr('Username is required'); return; }
    setErr('');
    setCredBusy(true);
    try {
      const r = await apiFetch<VerifyResp>(`/cameras/${camera.id}/onvif-credentials`, {
        method: 'PUT',
        body: JSON.stringify({
          username: user.trim(), password: pass, onvif_port: Number(port) || null,
        }),
      });
      const who = [r.vendor, r.model].filter(Boolean).join(' ') || 'device answered';
      setCredOut(`Verified: ${who} (port ${r.onvif_port}) — credentials saved.`);
      toast('ONVIF credentials verified & saved');
      onChanged();  // unlocks the other three editors
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setCredBusy(false);
    }
  }

  async function syncTime() {
    setErr('');
    setSyncBusy(true);
    try {
      const r = await apiFetch<SyncResp>(`/cameras/${camera.id}/sync-time`, { method: 'POST' });
      const s = r.skew_before_seconds;
      setSyncOut(s == null
        ? 'Clock set (camera did not report its previous time).'
        : `Camera clock was ${Math.abs(s).toFixed(1)}s ${s > 0 ? 'ahead' : 'behind'} — now synced.`);
      toast('Camera clock synced');
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setSyncBusy(false);
    }
  }

  async function reboot() {
    if (!confirm(`Reboot camera "${camera.name}"? The stream will drop for ~30–90 seconds.`)) return;
    setErr('');
    setRebootBusy(true);
    try {
      await apiFetch(`/cameras/${camera.id}/onvif-reboot`, { method: 'POST' });
      // Left disabled deliberately — the camera is going away for a minute and
      // a second reboot click would be meaningless.
      setRebooted(true);
      toast('Reboot requested — the camera will be back within a minute or two');
    } catch (e: any) {
      setErr(e.message);
      setRebootBusy(false);
    }
  }

  return (
    <Modal open title={`Maintenance — ${camera.name}`} onClose={onClose} width={560}>
      {/* ONVIF credentials */}
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-title" style={{ marginBottom: 8 }}>ONVIF credentials</div>
        <div className="d-hint" style={{ marginBottom: 10 }}>
          {camera.onvif_capable
            ? 'Credentials are stored for this camera — re-enter them here if they changed or no longer decrypt.'
            : 'None stored for this camera — enter them to enable the other device settings.'}
          {' '}Verified against the camera before saving.
        </div>
        <div className="form-grid">
          <div className="form-group">
            <Label>Username</Label>
            <input value={user} autoComplete="off" onChange={e => setUser(e.target.value)} />
          </div>
          <div className="form-group">
            <Label>Password</Label>
            <input type="password" autoComplete="new-password" value={pass}
                   onChange={e => setPass(e.target.value)} />
          </div>
          <div className="form-group">
            <Label>ONVIF port</Label>
            <input type="number" value={port} onChange={e => setPort(e.target.value)} />
          </div>
        </div>
        <div className="form-actions">
          <button className="btn-primary btn-sm" onClick={saveCreds} disabled={credBusy}>
            {credBusy ? <><span className="spinner" /> Verifying…</> : 'Verify & save'}
          </button>
        </div>
        {credOut && (
          <div style={{ fontSize: 12, color: 'var(--green)', marginTop: 8 }}>{credOut}</div>
        )}
      </div>

      {/* Clock sync */}
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="card-title" style={{ marginBottom: 8 }}>Sync clock</div>
        <div className="d-hint" style={{ marginBottom: 10 }}>
          Sets the camera’s clock to the server’s. Fixes drifting burned-in timestamps, and the clock skew that
          makes ONVIF authentication fail intermittently.
        </div>
        <button className="btn-ghost btn-sm" onClick={syncTime} disabled={syncBusy}>
          {syncBusy ? <span className="spinner" /> : 'Sync now'}
        </button>
        {syncOut && (
          <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>{syncOut}</div>
        )}
      </div>

      {/* Reboot */}
      <div className="card">
        <div className="card-title" style={{ marginBottom: 8 }}>Reboot camera</div>
        <div className="d-hint" style={{ marginBottom: 10 }}>
          Power-cycles the camera over ONVIF. Recording and live view drop until it comes back.
        </div>
        <button className="btn-danger btn-sm" onClick={reboot} disabled={rebooted || rebootBusy}>
          {rebooted ? 'Rebooting…' : rebootBusy ? <span className="spinner" /> : 'Reboot'}
        </button>
      </div>

      {err && <div style={{ color: 'var(--red)', fontSize: 12.5, marginTop: 10 }}>{err}</div>}

      <div className="form-actions">
        <button className="btn-ghost" onClick={onClose}>Close</button>
      </div>
    </Modal>
  );
}
