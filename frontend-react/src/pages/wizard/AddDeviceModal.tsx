/**
 * AddDeviceModal.tsx — the wizard's "add this device" dialog: confirm the name,
 * zone, retention and lawful basis for one discovered device, then create it.
 *
 * Two components because the inner one must mount fresh per device — its draft
 * state is seeded from the device and must not survive a switch to another.
 */
import { useEffect, useState } from 'react';

import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import {
  DSC_META, LAWFUL_BASES, bestProfile, complianceReady, defaultName, dscIsCamera,
  numberedName, wzCompliance, wzMeta,
  type AssignState, type Device,
} from './useDiscovery';

export function AddModal({ device, assign, onClose, onAdded }: {
  device: Device | null;
  assign: AssignState;
  onClose: () => void;
  onAdded: (name: string, id: string) => void;
}) {
  if (!device) return null;
  return <AddModalInner key={device.id} device={device} assign={assign} onClose={onClose} onAdded={onAdded} />;
}

export function AddModalInner({ device, assign, onClose, onAdded }: {
  device: Device;
  assign: AssignState;
  onClose: () => void;
  onAdded: (name: string, id: string) => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(defaultName(device));
  const [profile, setProfile] = useState(String(bestProfile(device)));
  const [recording, setRecording] = useState(assign.recording);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!complianceReady(assign)) {
      setErr('Select a lawful basis and enter a purpose before adding (DPDP)'); return;
    }
    setErr('');
    setBusy(true);
    try {
      const d = await apiFetch<Device>(`/discovery/devices/${device.id}/add`, {
        method: 'POST',
        body: JSON.stringify({
          name: name.trim(),
          profile_index: parseInt(profile, 10) || 0,
          enabled: true,
          recording: recording === 'true',
          zone: assign.zone.trim() || null,
          metadata: wzMeta(assign),
          ...wzCompliance(assign),
        }),
      });
      toast(`${d.ip} added to relay${d.error ? ' — ' + d.error : ''}`, d.error ? 'err' : 'ok');
      onAdded(name.trim() || d.ip || '', d.id);
      onClose();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  }

  return (
    <Modal open title="Add to RTSP relay" onClose={onClose} width={440}>
      <div style={{ color: 'var(--muted)', fontSize: 12, marginBottom: 14 }}>
        {device.ip} — {[device.vendor, device.model].filter(Boolean).join(' ')}
      </div>
      <div className="form-grid">
        <div className="form-group full">
          <label>Camera name</label>
          <input value={name} onChange={e => setName(e.target.value)} />
        </div>
        <div className="form-group full">
          <label>Profile</label>
          <select value={profile} onChange={e => setProfile(e.target.value)}>
            {(device.rtsp_candidates || []).map((c, i) => (
              <option key={i} value={String(i)}>
                {c.profile || c.token}{c.verified === true ? ' ✓' : c.verified === false ? ' (unverified)' : ''}
              </option>
            ))}
          </select>
        </div>
        <div className="form-group full">
          <label>Recording (NVR)</label>
          <select value={recording} onChange={e => setRecording(e.target.value as 'true' | 'false')}>
            <option value="true">On — record continuously</option>
            <option value="false">Off — live view only</option>
          </select>
        </div>
      </div>
      <div style={{ color: 'var(--red)', fontSize: 12, minHeight: 16, marginTop: 10 }}>{err}</div>
      <div className="form-actions">
        <button className="btn-ok" disabled={busy || !complianceReady(assign)} onClick={submit}
          title={!complianceReady(assign) ? 'Select a lawful basis and enter a purpose first (DPDP)' : undefined}>Add camera</button>
        <button className="btn-ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}
