/**
 * StepCredentials.tsx — wizard step 2: apply-to-all credentials box with
 * "Test all" (sequential PUT …/credentials with progress, legacy
 * dscBulkCreds), per-device rows (WZ_CRED_META), and the Set-credentials
 * modal (dscOpenCreds/dscSubmitCreds) — also reused by step 3's table.
 */
import { useState } from 'react';
import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import { DSC_META, dscIsCamera, type Device } from './useDiscovery';

/** status → [dot color, label, action label] (legacy WZ_CRED_META). */
const WZ_CRED_META: Record<string, [string, string, string | null]> = {
  verified:    ['var(--green)',  '✓ Ready',        null],
  added:       ['var(--accent)', 'In relay',       null],
  auth_failed: ['var(--red)',    'Wrong password', 'Set creds'],
  discovered:  ['#2E8FA8',       'Needs creds',    'Set creds'],
  probing:     ['var(--yellow)', 'Testing…',       null],
  no_onvif:    ['var(--yellow)', 'No ONVIF yet',   'Set creds'],
  unreachable: ['var(--dim)',    'Unreachable',    null],
};

/** Statuses "Test all" targets — every device still needing credentials. */
const CRED_TARGETS = ['auth_failed', 'discovered', 'no_onvif'];

export function CredsModal({ device, onClose, onSaved }: {
  device: Device | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  if (!device) return null;
  return <CredsModalInner key={device.id} device={device} onClose={onClose} onSaved={onSaved} />;
}

function CredsModalInner({ device, onClose, onSaved }: {
  device: Device;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [user, setUser] = useState(device.username || 'admin');
  const [pass, setPass] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit() {
    setErr('');
    setBusy(true);
    try {
      const d = await apiFetch<Device>(`/discovery/devices/${device.id}/credentials`, {
        method: 'PUT',
        body: JSON.stringify({ username: user.trim(), password: pass }),
      });
      toast(`${d.ip}: ${DSC_META[d.status]?.[2] || d.status}`, d.status === 'verified' ? 'ok' : 'err');
      onSaved();
      onClose();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <Modal open title="Set credentials" onClose={onClose} width={440}>
      <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 14 }}>
        {device.ip} — {device.vendor || 'unknown device'}
      </div>
      <div className="form-grid">
        <div className="form-group full">
          <label>Username</label>
          <input value={user} onChange={e => setUser(e.target.value)} />
        </div>
        <div className="form-group full">
          <label>Password</label>
          <input type="password" value={pass} onChange={e => setPass(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submit(); }} />
        </div>
      </div>
      <div style={{ color: 'var(--red)', fontSize: 12, minHeight: 16, marginTop: 10 }}>{err}</div>
      <div className="form-actions">
        <button className="btn-primary" disabled={busy} onClick={submit}>
          {busy ? <><span className="spinner" /> Probing…</> : 'Save & probe'}
        </button>
        <button className="btn-ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}

export function StepCredentials({ devices, loadDevices }: {
  devices: Device[];
  loadDevices: () => Promise<void>;
}) {
  const toast = useToast();
  const [user, setUser] = useState('admin');
  const [pass, setPass] = useState('');
  const [prog, setProg] = useState<{ done: number; total: number } | null>(null);
  const [credDev, setCredDev] = useState<Device | null>(null);

  const cams = devices.filter(d => dscIsCamera(d) && d.status !== 'ignored');

  // Prototype "Test all": sequential credentialed re-probes — including
  // no_onvif devices, whose port sweep may simply have been swallowed.
  async function testAll() {
    if (!pass) { toast('Enter a password to apply', 'err'); return; }
    const targets = devices.filter(d => CRED_TARGETS.includes(d.status));
    if (!targets.length) { toast('No matching devices', 'err'); return; }
    let ok = 0, done = 0;
    setProg({ done: 0, total: targets.length });
    for (const d of targets) {
      try {
        const r = await apiFetch<Device>(`/discovery/devices/${d.id}/credentials`, {
          method: 'PUT',
          body: JSON.stringify({ username: user.trim(), password: pass }),
        });
        if (r.status === 'verified') ok++;
      } catch { /* keep probing the rest */ }
      done++;
      setProg({ done, total: targets.length });
    }
    setProg(null);
    toast(`Done — ${ok} of ${targets.length} now ready`, 'ok');
    loadDevices();
  }

  return (
    <div>
      <div className="wz-h" style={{ marginBottom: 5 }}>
        {cams.length} camera{cams.length === 1 ? '' : 's'} discovered
      </div>
      <div className="wz-sub">
        Set credentials below and test. Cameras with credential errors stay listed until fixed — they're kept as
        drafts, nothing is lost.
      </div>
      <div className="wz-credbox">
        <div style={{ flex: 1 }}>
          <label style={{ fontSize: 11, color: 'var(--muted)' }}>Username (apply to all)</label>
          <input value={user} onChange={e => setUser(e.target.value)} style={{ marginTop: 5 }} />
        </div>
        <div style={{ flex: 1 }}>
          <label style={{ fontSize: 11, color: 'var(--muted)' }}>Password</label>
          <input type="password" value={pass} onChange={e => setPass(e.target.value)}
            placeholder="enter password" style={{ marginTop: 5 }} />
        </div>
        <button className="wz-testall" disabled={!!prog} onClick={testAll}>
          {prog ? <><span className="spinner" /> Probing {prog.done}/{prog.total}…</> : 'Test all'}
        </button>
      </div>
      <div className="wz-rows">
        {!cams.length && (
          <p style={{ color: 'var(--dim)', fontSize: 13 }}>No devices discovered yet — run a scan in step 1.</p>
        )}
        {cams.map(d => {
          const [dot, label, action] = WZ_CRED_META[d.status] || ['var(--dim)', d.status, null];
          return (
            <div key={d.id} className="wz-row"
              style={{ borderColor: d.status === 'auth_failed' ? 'rgba(220,38,38,.4)' : 'var(--border2)' }}>
              <span className="wz-dot" style={{ background: dot }} />
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--muted)', minWidth: 105 }}>
                {d.ip || '—'}
              </span>
              <span style={{ fontSize: 12, color: 'var(--text2)', flex: 1 }}>
                {[d.vendor, d.model].filter(Boolean).join(' ') || 'Unknown device'}
              </span>
              <span style={{ fontSize: 11, color: dot }}>{label}</span>
              {action && (
                <button className="btn-ghost btn-sm" onClick={() => setCredDev(d)}>{action}</button>
              )}
            </div>
          );
        })}
      </div>
      <CredsModal device={credDev} onClose={() => setCredDev(null)} onSaved={loadDevices} />
    </div>
  );
}
