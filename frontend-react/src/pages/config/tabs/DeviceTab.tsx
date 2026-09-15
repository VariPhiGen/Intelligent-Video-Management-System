/**
 * DeviceTab.tsx — ONVIF device-settings hub (legacy openStreamSettings / csShow).
 * Four editors, each in its own modal: encoder / imaging / OSD / maintenance.
 *
 * The three read-only-until-authenticated editors deep-link into Maintenance
 * when the camera rejects the stored credentials, because that is the only
 * thing the operator can actually do about it (legacy csFail).
 */
import { useState } from 'react';
import type { Camera } from '@/lib/types';
import { EncoderModal } from '../device/EncoderModal';
import { ImagingModal } from '../device/ImagingModal';
import { OsdModal } from '../device/OsdModal';
import { MaintenanceModal } from '../device/MaintenanceModal';
import { CertificationModal } from '../device/CertificationModal';
import { StqcBadge } from '@/components/StqcBadge';

type Key = 'encoder' | 'imaging' | 'osd' | 'maintenance' | 'certification';

const CARDS: { key: Key; icon: string; title: string; sub: string }[] = [
  { key: 'encoder', icon: '▥', title: 'Stream settings', sub: 'Resolution, fps, bitrate, GOP' },
  { key: 'imaging', icon: '☀', title: 'Image', sub: 'Brightness, WDR, exposure, day/night' },
  { key: 'osd', icon: '𝑻', title: 'OSD overlays', sub: 'Burned-in text & timestamps' },
  { key: 'maintenance', icon: '⚙', title: 'Maintenance', sub: 'Reboot, clock sync, credentials' },
  { key: 'certification', icon: '⛨', title: 'Certification', sub: 'STQC / BIS-ER compliance posture' },
];

export function DeviceTab({ camera, onRefresh }: { camera: Camera; onRefresh?: () => void }) {
  const [openKey, setOpenKey] = useState<Key | null>(null);
  const close = () => setOpenKey(null);
  const toMaintenance = () => setOpenKey('maintenance');

  return (
    <>
      <div className="d-hint" style={{ marginBottom: 12 }}>
        {camera.onvif_capable
          ? 'ONVIF connected — device settings are managed from here.'
          : 'No ONVIF credentials stored yet — enter them under Maintenance to unlock device settings. Certification is recorded against the camera record and stays editable.'}
      </div>
      <div className="wz-methods" style={{ gridTemplateColumns: 'repeat(auto-fit,minmax(190px,1fr))' }}>
        {CARDS.map(c => (
          <div key={c.key} className="wz-method" onClick={() => setOpenKey(c.key)}>
            <div className="wz-mi">{c.icon}</div>
            <b>{c.title}</b>
            {/* Certification reads from the camera record, not the device, so
                its state is known even when ONVIF is unreachable — show it
                rather than the generic subtitle. */}
            {c.key === 'certification'
              ? <span style={{ marginTop: 2 }}><StqcBadge camera={camera} /></span>
              : <span>{c.sub}</span>}
          </div>
        ))}
      </div>

      {openKey === 'encoder' && (
        <EncoderModal camera={camera} onClose={close} onOpenMaintenance={toMaintenance} />
      )}
      {openKey === 'imaging' && (
        <ImagingModal camera={camera} onClose={close} onOpenMaintenance={toMaintenance} />
      )}
      {openKey === 'osd' && (
        <OsdModal camera={camera} onClose={close} onOpenMaintenance={toMaintenance} />
      )}
      {openKey === 'maintenance' && (
        <MaintenanceModal camera={camera} onClose={close} onChanged={() => onRefresh?.()} />
      )}
      {openKey === 'certification' && (
        <CertificationModal camera={camera} onClose={close} onChanged={() => onRefresh?.()} />
      )}
    </>
  );
}
