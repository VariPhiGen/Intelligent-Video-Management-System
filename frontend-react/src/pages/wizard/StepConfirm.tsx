/**
 * StepConfirm.tsx — wizard step 5: onboarding summary (legacy
 * wzRenderConfirm) — batch chips, per-camera rows with Masks/Live shortcuts,
 * and the exit actions (inventory / live view / reset).
 */
import { useNavigate } from 'react-router-dom';
import type { Camera } from '@/lib/types';
import { type AddedCam, type AssignState } from './useDiscovery';

export function StepConfirm({ added, cameras, assign, onReset }: {
  added: AddedCam[];
  cameras: Camera[];
  assign: AssignState;
  onReset: () => void;
}) {
  const nav = useNavigate();
  const n = added.length;
  const recording = assign.recording !== 'false';
  const masked = added.filter(c => {
    const cam = cameras.find(x => x.id === c.id);
    return cam && (cam.privacy_masks || []).length;
  }).length;

  // Batch summary chips: zone · recording · lawful basis · masks drawn.
  const chips: string[] = [];
  if (n) {
    if (assign.zone.trim()) chips.push(`⬡ ${assign.zone.trim()}`);
    chips.push(recording ? '⏺ Recording on' : 'Recording off');
    if (assign.lawful.trim()) chips.push(`⚖ ${assign.lawful.trim()}`);
    chips.push(masked ? `▱ ${masked} with masks` : '▱ no masks drawn');
  }

  return (
    <div>
      <div style={{ textAlign: 'center', padding: '16px 10px 0' }}>
        <div className="wz-check">✓</div>
        <h1 style={{ fontSize: 18 }}>{n} camera{n === 1 ? '' : 's'} onboarded</h1>
        <p className="subtitle" style={{ marginBottom: 14 }}>
          {n
            ? "They're live in the relay and syncing to the NVR."
            : 'Nothing added in this session yet — go back to step 1 to onboard cameras.'}
        </p>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'center', flexWrap: 'wrap', marginBottom: 18 }}>
          {chips.map(c => <span key={c} className="chip" style={{ cursor: 'default' }}>{c}</span>)}
        </div>
      </div>
      <div className="wz-rows" style={{ maxWidth: 620, margin: '0 auto' }}>
        {added.map(c => {
          const cam = cameras.find(x => x.id === c.id);
          return (
            <div key={c.id} className="wz-row">
              <span style={{ color: 'var(--green)', fontWeight: 700 }}>✓</span>
              <b style={{ flex: 1 }}>{c.name}</b>
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>
                {cam ? (cam.ip || cam.slug) : ''}
              </span>
              <button className="btn-ghost btn-sm"
                onClick={() => nav(`/cameras/config?cam=${c.id}&tab=recording`)}>
                Masks
              </button>
              {cam && (
                <button className="btn-ghost btn-sm" onClick={() => nav(`/live?cam=${c.id}`)}>Live</button>
              )}
            </div>
          );
        })}
      </div>
      <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 20 }}>
        <button className="btn-primary" onClick={() => nav('/cameras')}>View in inventory →</button>
        <button className="btn-ghost" onClick={() => nav('/live')}>Open Live View</button>
        <button className="btn-ghost" onClick={onReset}>Add more cameras</button>
      </div>
    </div>
  );
}
