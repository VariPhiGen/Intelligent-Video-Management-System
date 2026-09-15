/**
 * PrivacyTab.tsx — privacy masking as a config tab: draw-on-frame editor on the
 * left, the list of masks on the right. Drawing is the shared <PolygonEditor>
 * (normalized 0–1 coords over an authenticated snapshot); the NVR blurs each
 * polygon into recordings. The add-camera wizard embeds this tab and drives it
 * through the exposed MaskEditorHandle (unsaved-work detection + save-on-leave).
 */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import { PolygonEditor, type EditorPolygon } from '@/components/PolygonEditor';

const MASK_COLORS = ['#ef4444', '#3b82f6', '#22c55e', '#a855f7', '#f59e0b', '#ec4899'];

/** Imperative view of the editor for hosts that own the step navigation (the
 *  add-camera wizard): is there unsaved work, and a way to save it. Without
 *  this, leaving the wizard's mask step silently discarded drawn masks. */
export interface MaskEditorHandle {
  dirty: boolean;
  pendingShape: boolean;
  save: () => Promise<boolean>;
}

export function PrivacyTab({ camera, onSaved, expose }: {
  camera: Camera;
  onSaved: () => void;
  expose?: (h: MaskEditorHandle | null) => void;
}) {
  const toast = useToast();
  const [masks, setMasks] = useState<number[][][]>([]);
  const [pendingPts, setPendingPts] = useState(0);   // in-progress shape point count
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState('');

  // Re-init on camera switch only (edits survive the 15s data refresh).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    setMasks(JSON.parse(JSON.stringify(camera.privacy_masks || [])) as number[][][]);
    setErr('');
  }, [camera.id]);

  const polygons: EditorPolygon[] = masks.map((poly, i) => ({
    points: poly, color: MASK_COLORS[i % MASK_COLORS.length], label: `Mask ${i + 1}`,
  }));

  async function save(): Promise<boolean> {
    if (pendingPts > 0) { toast('Finish or cancel the shape you’re drawing first', 'err'); return false; }
    setSaving(true); setErr('');
    let ok = false;
    try {
      await apiFetch(`/cameras/${camera.id}/masks`, { method: 'PUT', body: JSON.stringify({ masks }) });
      toast(masks.length
        ? `${masks.length} mask${masks.length === 1 ? '' : 's'} saved — recording restarting with masking`
        : 'Masks cleared — recording restarting without masking');
      onSaved();
      ok = true;
    } catch (e: any) { setErr(e.message); toast(e.message, 'err'); }
    setSaving(false);
    return ok;
  }

  // Unsaved-changes handle for the wizard (see MaskEditorHandle). Re-exposed
  // every render so `dirty`/`save` always reflect current state.
  const dirty = JSON.stringify(masks) !== JSON.stringify(camera.privacy_masks || []);
  useEffect(() => {
    expose?.({ dirty, pendingShape: pendingPts > 0, save });
    return () => expose?.(null);
  });

  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 16, alignItems: 'start' }}>
        {/* Draw canvas (shared editor) */}
        <div>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 8 }}>
            <span className="card-title">Draw privacy masks</span>
            <span style={{ fontSize: 12, color: 'var(--muted)' }}>Click the frame to drop points</span>
          </div>
          <PolygonEditor cameraId={camera.id} polygons={polygons} drawing noun="mask"
            onFinish={pts => setMasks(m => [...m, pts])} onPendingChange={setPendingPts} />
        </div>

        {/* Mask list side panel */}
        <div className="card">
          <div className="card-title" style={{ marginBottom: 10 }}>Masks · {masks.length}</div>
          {!masks.length && !pendingPts && (
            <div style={{ fontSize: 12.5, color: 'var(--dim)' }}>
              No masks yet. Click points on the frame to outline an area, then Finish mask.
            </div>
          )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {masks.map((poly, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 10, padding: '8px 10px',
                border: '1px solid var(--border)', borderRadius: 8,
              }}>
                <span style={{ width: 12, height: 12, borderRadius: 3, flexShrink: 0,
                               background: MASK_COLORS[i % MASK_COLORS.length] }} />
                <span style={{ fontSize: 12.5, flex: 1 }}>Mask {i + 1}</span>
                <span style={{ fontSize: 11, color: 'var(--dim)', fontFamily: 'var(--mono)' }}>{poly.length} pts</span>
                <button className="btn-ghost btn-sm" title="Remove"
                        onClick={() => setMasks(m => m.filter((_, j) => j !== i))}>×</button>
              </div>
            ))}
          </div>
          <div style={{
            marginTop: 12, padding: '10px 12px', borderRadius: 8,
            background: 'var(--accentSoft)', color: 'var(--accent2)', fontSize: 12, lineHeight: 1.5,
          }}>
            Masked areas are <b>blurred into recordings</b> by the NVR (it re-encodes this camera). Live view is
            not masked, and footage already recorded is unchanged. Saving restarts recording for a few seconds.
          </div>
        </div>
      </div>

      {err && <div style={{ color: 'var(--red)', fontSize: 12.5, marginTop: 10 }}>{err}</div>}

      <div className="form-actions" style={{ marginTop: 16 }}>
        <button className="btn-primary" disabled={saving} onClick={save}>
          {saving ? 'Saving…' : 'Save configuration'}
        </button>
      </div>
    </div>
  );
}
