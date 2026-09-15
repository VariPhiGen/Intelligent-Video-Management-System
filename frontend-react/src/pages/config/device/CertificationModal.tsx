/**
 * CertificationModal.tsx — STQC / BIS-ER certification posture for one camera.
 *
 * India requires Essential Requirements / STQC certification for CCTV cameras
 * SOLD from 1 April 2026. This records posture; it never blocks anything. An
 * existing uncertified fleet is lawful to operate, so refusing to save an
 * uncertified camera would only push operators into lying to the form.
 *
 * The default is 'unknown' and stays 'unknown' until someone asserts otherwise —
 * the whole value of the fleet report is that "nobody checked" is countable and
 * distinguishable from "checked and compliant". Nothing here infers a status
 * from vendor, model, or firmware.
 *
 * Unlike its neighbours in this folder, this talks to the camera *record*
 * (PUT /cameras/{id}), not to the device over ONVIF — a certificate is a
 * document about the hardware, not a setting on it. So it needs no credentials
 * and works on an unreachable camera.
 */
import { useState } from 'react';
import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import type { Camera, StqcStatus } from '@/lib/types';
import { STQC_LABELS } from '@/lib/types';
import { Label } from './onvif';

/** Order shown in the picker: the affirmative answers first, 'unknown' last as
 *  the retraction. Mirrors STQC_STATUSES in models.py. */
const OPTIONS: { value: StqcStatus; hint: string }[] = [
  { value: 'certified', hint: 'Holds a valid ER/STQC certificate' },
  { value: 'not_certified', hint: 'Checked — no valid certificate' },
  { value: 'exempt', hint: 'Outside the mandate (analogue, non-networked)' },
  { value: 'not_applicable', hint: 'Not procured in India / pre-dates the mandate' },
  { value: 'unknown', hint: 'Not yet verified' },
];

export function CertificationModal({ camera, onClose, onChanged }: {
  camera: Camera;
  onClose: () => void;
  onChanged?: () => void;
}) {
  const toast = useToast();
  const [status, setStatus] = useState<StqcStatus>(camera.stqc_status || 'unknown');
  const [certNo, setCertNo] = useState(camera.stqc_certificate_no ?? '');
  const [validUntil, setValidUntil] = useState(camera.stqc_valid_until ?? '');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  // Certificate details only mean something behind an affirmative status.
  // Showing them under 'not_certified' invites recording the number of a
  // certificate the camera does not hold.
  const wantsCert = status === 'certified';

  const dirty =
    status !== (camera.stqc_status || 'unknown') ||
    certNo !== (camera.stqc_certificate_no ?? '') ||
    validUntil !== (camera.stqc_valid_until ?? '');

  async function save() {
    setBusy(true);
    setErr('');
    try {
      await apiFetch(`/cameras/${camera.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          stqc_status: status,
          // Clear the certificate details when the status no longer supports
          // them, so a downgrade can't leave a stale number behind on the row.
          stqc_certificate_no: wantsCert ? certNo.trim() : '',
          stqc_valid_until: wantsCert && validUntil ? validUntil : null,
        }),
      });
      toast('Certification updated');
      onChanged?.();
      onClose();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      title="STQC / BIS-ER certification"
      onClose={onClose}
      width={560}
      footer={
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {camera.stqc_verified_by && (
            <span style={{ fontSize: 11, color: 'var(--dim)' }}>
              Attested by {camera.stqc_verified_by}
              {camera.stqc_verified_at
                ? ` on ${new Date(camera.stqc_verified_at).toLocaleDateString()}`
                : ''}
            </span>
          )}
          <div style={{ flex: 1 }} />
          <button className="btn-subtle btn-sm" onClick={onClose} disabled={busy}>Cancel</button>
          <button className="btn btn-sm" onClick={save} disabled={busy || !dirty}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      }
    >
      <div className="d-hint" style={{ marginBottom: 14 }}>
        Cameras sold in India from 1 April 2026 must hold Essential Requirements
        / STQC certification. Recording this does not change how the camera runs —
        it makes the fleet's compliance position reportable.
      </div>

      {err && <div className="err" style={{ marginBottom: 12 }}>{err}</div>}

      <div style={{ marginBottom: 14 }}>
        <Label hint="never inferred">Certification status</Label>
        <div style={{ display: 'grid', gap: 6, marginTop: 6 }}>
          {OPTIONS.map(o => (
            <label
              key={o.value}
              style={{
                display: 'flex', alignItems: 'baseline', gap: 8, padding: '7px 10px',
                border: '1px solid var(--border2)', borderRadius: 6, cursor: 'pointer',
                background: status === o.value ? 'var(--hover)' : 'transparent',
                fontWeight: 400,
              }}
            >
              <input
                type="radio"
                name="stqc"
                checked={status === o.value}
                onChange={() => setStatus(o.value)}
              />
              <b style={{ fontSize: 13 }}>{STQC_LABELS[o.value]}</b>
              <span style={{ fontSize: 11, color: 'var(--dim)' }}>{o.hint}</span>
            </label>
          ))}
        </div>
      </div>

      {wantsCert && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 160px', gap: 12 }}>
          <div>
            <Label hint="as printed">Certificate number</Label>
            <input
              value={certNo}
              onChange={e => setCertNo(e.target.value)}
              placeholder="e.g. STQC/ER/2026/01234"
              maxLength={128}
              style={{ width: '100%' }}
            />
          </div>
          <div>
            <Label hint="optional">Valid until</Label>
            <input
              type="date"
              value={validUntil}
              onChange={e => setValidUntil(e.target.value)}
              style={{ width: '100%' }}
            />
          </div>
        </div>
      )}
    </Modal>
  );
}
