/**
 * StepMasks.tsx — wizard step 4 (optional): draw privacy masks INLINE, right in
 * the onboarding flow — no redirect to the config page. Pick a camera added this
 * session, mask it on the frame, save, move on. Masks can still be edited later
 * from Cameras → Configuration → Privacy.
 */
import { useRef, useState } from 'react';
import type { Camera } from '@/lib/types';
import type { AddedCam } from './useDiscovery';
import { PrivacyTab, type MaskEditorHandle } from '@/pages/config/tabs/PrivacyTab';

export function StepMasks({ added, cameras, refresh, expose }: {
  added: AddedCam[];
  cameras: Camera[];
  refresh: () => void;
  /** Surfaces the editor's unsaved-changes handle so the wizard's Continue
   *  can auto-save drawn masks instead of silently discarding them. */
  expose?: (h: MaskEditorHandle | null) => void;
}) {
  const [sel, setSel] = useState<string | null>(added[0]?.id ?? null);
  // Keep the selection valid as `added` grows / the picked one drops out.
  const selId = sel && added.some(a => a.id === sel) ? sel : (added[0]?.id ?? null);
  const cam = cameras.find(c => c.id === selId) ?? null;
  // Mirror the exposed handle locally so switching cameras also auto-saves
  // (the key= remount would otherwise discard unsaved masks the same way
  // leaving the step used to).
  const handleRef = useRef<MaskEditorHandle | null>(null);
  const exposeBoth = (h: MaskEditorHandle | null) => { handleRef.current = h; expose?.(h); };

  async function pick(id: string) {
    if (id === selId) return;
    const h = handleRef.current;
    if (h && (h.dirty || h.pendingShape)) {
      if (!(await h.save())) return;   // save() surfaces its own toast on failure
    }
    setSel(id);
  }

  return (
    <div>
      <div className="wz-h">Privacy masks (optional)</div>
      <div className="wz-sub">
        Outline any region that must never be recorded — it's blurred into every segment. Draw it here; there's no
        need to leave the wizard. You can also do this any time from Cameras → Configuration → Privacy.
      </div>

      {!added.length && (
        <p style={{ color: 'var(--dim)', fontSize: 13 }}>
          No cameras added in this session yet — add some first, or skip: masks can be drawn any time.
        </p>
      )}

      {/* One camera → straight to its editor; several → pick which to mask. */}
      {added.length > 1 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 14 }}>
          {added.map(a => {
            const masks = cameras.find(c => c.id === a.id)?.privacy_masks?.length ?? 0;
            return (
              <button key={a.id} className={`chip${selId === a.id ? ' active' : ''}`}
                      onClick={() => pick(a.id)}>
                {a.name}
                {masks > 0 && <span className="badge-dot" style={{ background: 'var(--green)' }} />}
              </button>
            );
          })}
        </div>
      )}

      {/* key=cam.id remounts the editor (fresh snapshot + masks) on switch. */}
      {cam && <PrivacyTab key={cam.id} camera={cam} onSaved={refresh} expose={exposeBoth} />}
      {selId && !cam && (
        <div className="empty">Preparing the camera… (it just registered — one moment).</div>
      )}
    </div>
  );
}
