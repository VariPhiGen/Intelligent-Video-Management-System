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
  ip: string;
  onvif_port: number;
  onvif_use_https: boolean;
}
interface SyncResp {
  skew_before_seconds: number | null;
  synced_to: string;
}

/** Host portion of an RTSP URL — the box the stream is pulled from, which is
 *  the camera itself only when nothing re-publishes it. */
function streamHost(url: string | null | undefined): string {
  if (!url) return '';
  const m = /^rtsps?:\/\/(?:[^@/]*@)?(\[[^\]]+\]|[^:/?#]+)/i.exec(url);
  return m ? m[1] : '';
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
  // The camera's OWN address, which is not always the RTSP URL's host: a stream
  // re-published by a relay or an NVR channel arrives from that box, and ONVIF
  // has to go to the camera. Editable here because a camera registered by such a
  // URL has the wrong address stored, and nothing else can correct it.
  const [ip, setIp] = useState(camera.ip || streamHost(camera.rtsp_url) || '');
  const [useHttps, setUseHttps] = useState(camera.onvif_use_https);
  const [credBusy, setCredBusy] = useState(false);
  const [credOut, setCredOut] = useState('');

  const [syncBusy, setSyncBusy] = useState(false);
  const [syncOut, setSyncOut] = useState('');

  const [rebooted, setRebooted] = useState(false);
  const [rebootBusy, setRebootBusy] = useState(false);

  const [err, setErr] = useState('');

  // A camera that has never answered ONVIF has an address nobody checked: it
  // was taken from the RTSP URL, which is the camera only when nothing
  // re-publishes the stream. Say so, because a relay's host in this field is
  // exactly how these settings come to fail with a connection refused against a
  // host the operator never typed.
  const sHost = streamHost(camera.rtsp_url);
  const guessed = !camera.onvif_capable && !!sHost && sHost === ip.trim();

  async function saveCreds() {
    if (!user.trim()) { setErr('Username is required'); return; }
    if (!ip.trim()) { setErr("The camera's address is required"); return; }
    setErr('');
    setCredBusy(true);
    try {
      const r = await apiFetch<VerifyResp>(`/cameras/${camera.id}/onvif-credentials`, {
        method: 'PUT',
        body: JSON.stringify({
          username: user.trim(), password: pass, onvif_port: Number(port) || null,
          ip: ip.trim(),
          onvif_use_https: useHttps,
        }),
      });
      const who = [r.vendor, r.model].filter(Boolean).join(' ') || 'device answered';
      const scheme = r.onvif_use_https ? 'https' : 'http';
      setCredOut(`Verified: ${who} at ${r.ip}:${r.onvif_port} (${scheme}) — credentials saved.`);
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
        {guessed && (
          <div className="d-hint" style={{ marginBottom: 10, color: 'var(--amber, var(--muted))' }}>
            The address below was taken from this camera’s RTSP URL and has never been
            verified. If that stream arrives from a relay or an NVR channel rather than
            the camera itself, replace it with the camera’s own IP — device settings
            cannot reach a relay.
          </div>
        )}
        <div className="form-grid">
          <div className="form-group">
            <Label>Camera address</Label>
            <input value={ip} autoComplete="off" spellCheck={false}
                   placeholder="192.168.1.64"
                   onChange={e => setIp(e.target.value)} />
            {sHost && sHost !== ip.trim() && (
              <div className="d-hint" style={{ marginTop: 4 }}>Stream arrives from {sHost}</div>
            )}
          </div>
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
        <label className="d-hint" style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8 }}>
          <input type="checkbox" checked={useHttps} onChange={e => {
            const checked = e.target.checked;
            setUseHttps(checked);
            // HTTPS is virtually always a different port than plain HTTP on the
            // same device (443 vs 80) — checking the box without also moving off
            // the HTTP port just points HTTPS at a port nothing is listening for
            // TLS on, which hangs until the request times out. Only nudge away
            // from the well-known default: a port the operator already changed
            // to something else (a non-standard HTTP port, or already 443) is
            // deliberate and must not be silently overwritten either direction.
            if (checked && port === '80') setPort('443');
            if (!checked && port === '443') setPort('80');
          }} />
          Camera requires HTTPS for ONVIF
        </label>
        <div className="d-hint" style={{ marginTop: 2, marginBottom: 4 }}>
          Leave unchecked unless PTZ/encoder/imaging keep failing while the video itself plays fine —
          that combination means this camera's firmware rejects ONVIF over plain HTTP. HTTPS is almost
          always a different port than plain HTTP on the same camera (443 is the usual default) —
          double-check the ONVIF port above still matches whichever scheme is checked.
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
