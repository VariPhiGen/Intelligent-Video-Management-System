/**
 * RegisterCoverage.tsx — calibrating one camera against the site plan.
 *
 * The operator clicks a landmark in the camera frame, then the same spot on the
 * plan, five times or more. That fits the frame -> plan homography which makes
 * the map able to say what ground a camera covers, and which a detection region
 * drawn once on the plan is later projected through.
 *
 * **Why a modal and not the side panel.** Landmark picking needs the frame and
 * the plan side by side at a usable size; the accuracy of the whole feature is
 * bounded by how precisely the operator can hit a pole base. Cramming that into
 * the right-hand column would quietly cost real metres on site.
 *
 * **Why no zoom here.** A ~900px panel showing a 1080p frame puts a screen
 * pixel at about two image pixels, so a ±2px click lands within ~0.4% of the
 * frame — comfortably inside the 2%-of-plan error budget the registration is
 * held to. Zoom would help a cluttered scene and is worth adding when one turns
 * up; it is not needed for the accuracy target.
 *
 * **The five-pin rule is the load-bearing part of this screen.** Four pairs fit
 * a homography exactly, so the error reads zero no matter where they were
 * clicked — a perfect-looking score on a registration that could be entirely
 * wrong. The fifth pin is the first one that checks the other four, and the UI
 * says so rather than just disabling a button.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { Modal } from '@/components/Modal';
import type { Camera, CameraRegistration, SitemapMeta } from '@/lib/types';

import {
  MAX_RMS, MIN_PAIRS,
  fitHomography, previewFootprint, reprojectionRms, suspectPin, type Pair,
} from './homographyFit';

/** A pin drawn over either image. Numbered so a frame pin and its plan pin are
 *  visibly the same landmark — the pairing is the thing the operator has to
 *  keep straight, so it is carried by the label, not by colour alone. */
function Pin({ x, y, n, tone, onRemove }: {
  x: number; y: number; n: number;
  tone: 'ok' | 'pending' | 'suspect';
  onRemove?: () => void;
}) {
  const background =
    tone === 'suspect' ? 'var(--danger, #d9534f)'
      : tone === 'pending' ? 'var(--warn, #e0a030)'
        : 'var(--accent)';
  return (
    <div
      onClick={onRemove ? (e => { e.stopPropagation(); onRemove(); }) : undefined}
      title={onRemove ? `Landmark ${n} — click to remove` : `Landmark ${n}`}
      style={{
        position: 'absolute', left: `${x * 100}%`, top: `${y * 100}%`,
        transform: 'translate(-50%,-50%)',
        width: 20, height: 20, borderRadius: '50%',
        background, color: '#fff',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 10.5, fontWeight: 700, border: '2px solid #fff',
        boxShadow: '0 1px 5px rgba(0,0,0,.45)',
        cursor: onRemove ? 'pointer' : 'default',
        pointerEvents: onRemove ? 'auto' : 'none',
      }}
    >{n}</div>
  );
}

/** One clickable image with its pins. Both panels are the same component
 *  because the interaction is identical — only which half of the pair it
 *  contributes differs. */
function PickPanel({ label, hint, src, pins, pending, suspect, armed, onPick, onRemove, children }: {
  label: string;
  hint: string;
  src: string | null;
  pins: { x: number; y: number }[];
  pending: { x: number; y: number } | null;
  suspect: number | null;
  armed: boolean;
  onPick: (x: number, y: number) => void;
  onRemove: (index: number) => void;
  children?: React.ReactNode;
}) {
  return (
    <div style={{ flex: '1 1 380px', minWidth: 280 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
        <span style={{ fontSize: 12.5, fontWeight: 650 }}>{label}</span>
        <span style={{ fontSize: 11.5, color: armed ? 'var(--accent)' : 'var(--dim)' }}>{hint}</span>
      </div>
      <div
        onClick={e => {
          const rect = e.currentTarget.getBoundingClientRect();
          if (!rect.width || !rect.height) return;
          onPick((e.clientX - rect.left) / rect.width, (e.clientY - rect.top) / rect.height);
        }}
        style={{
          position: 'relative', width: '100%', aspectRatio: '16 / 9',
          background: 'var(--sunk, #1b1f27)', borderRadius: 6, overflow: 'hidden',
          cursor: 'crosshair',
          // The armed panel is outlined so the operator always knows which half
          // of the pair the next click contributes. Losing track of that is the
          // one way to build a confidently mirrored registration.
          outline: armed ? '2px solid var(--accent)' : '1px solid var(--line, #2a2f38)',
          outlineOffset: armed ? -2 : -1,
        }}
      >
        {src
          ? <img src={src} alt={label} draggable={false}
                 style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />
          : <div style={{
              position: 'absolute', inset: 0, display: 'flex',
              alignItems: 'center', justifyContent: 'center',
              fontSize: 12, color: 'var(--dim)',
            }}>No image available</div>}
        {children}
        {pins.map((p, i) => (
          <Pin key={i} x={p.x} y={p.y} n={i + 1}
               tone={suspect === i ? 'suspect' : 'ok'}
               onRemove={() => onRemove(i)} />
        ))}
        {pending && <Pin x={pending.x} y={pending.y} n={pins.length + 1} tone="pending" />}
      </div>
    </div>
  );
}

export function RegisterCoverage({
  open, camera, plan, planImgUrl, frameUrl, existing, busy, onSave, onDelete, onClose,
}: {
  open: boolean;
  camera: Camera | null;
  plan: SitemapMeta | null;
  planImgUrl: string | null;
  /** The camera's current frame, already fetched by the page as an object URL. */
  frameUrl: string | null;
  existing: CameraRegistration | null;
  busy: boolean;
  onSave: (pairs: Pair[], homography: number[][]) => void;
  onDelete: (registrationId: number) => void;
  onClose: () => void;
}) {
  const [pairs, setPairs] = useState<Pair[]>([]);
  const [pendingFrame, setPendingFrame] = useState<{ x: number; y: number } | null>(null);

  // Reopening starts from whatever is stored, so "recalibrate" means adjusting
  // the existing landmarks rather than redoing all five from nothing.
  useEffect(() => {
    if (!open) return;
    setPairs(existing?.landmark_pairs ?? []);
    setPendingFrame(null);
  }, [open, existing]);

  // Escape cancels a half-placed pair before the Modal's own Escape closes the
  // dialog — losing four good pins to a mistyped fifth would be infuriating.
  useEffect(() => {
    if (!open || !pendingFrame) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); setPendingFrame(null); }
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [open, pendingFrame]);

  const fitted = useMemo(() => fitHomography(pairs), [pairs]);
  const rms = useMemo(() => (fitted ? reprojectionRms(fitted, pairs) : null), [fitted, pairs]);
  const suspect = useMemo(() => suspectPin(pairs), [pairs]);
  const preview = useMemo(() => (fitted ? previewFootprint(fitted) : []), [fitted]);

  const scaleM = plan?.calibration && existing?.reprojection_error
    ? existing.reprojection_error_m! / existing.reprojection_error
    : null;

  const onFramePick = useCallback((x: number, y: number) => {
    setPendingFrame({ x, y });
  }, []);

  const onPlanPick = useCallback((mx: number, my: number) => {
    setPendingFrame(current => {
      if (!current) return null;
      setPairs(list => [...list, { fx: current.x, fy: current.y, mx, my }]);
      return null;
    });
  }, []);

  const removePair = useCallback((index: number) => {
    setPairs(list => list.filter((_, i) => i !== index));
  }, []);

  const withinBudget = rms != null && Number.isFinite(rms) && rms <= MAX_RMS;
  const canSave = !busy && pairs.length >= MIN_PAIRS && fitted !== null && withinBudget;

  // One line that says what to do next, in the order the operator hits them.
  const status = (() => {
    if (pendingFrame) return { tone: 'accent', text: 'Now click the same spot on the plan.' };
    if (pairs.length < MIN_PAIRS) {
      const left = MIN_PAIRS - pairs.length;
      return {
        tone: 'dim',
        text: pairs.length < 4
          ? `Click a ground-level landmark in the frame, then the same spot on the plan. ${left} more to go.`
          : 'One more. Four landmarks always fit perfectly, so the fifth is the one that actually checks them.',
      };
    }
    if (!fitted) {
      return { tone: 'err', text: 'These landmarks are too close to a straight line — spread them across the view.' };
    }
    if (!Number.isFinite(rms!)) {
      return { tone: 'err', text: 'A landmark sits on the horizon. Move it onto visible ground.' };
    }
    if (!withinBudget) {
      return {
        tone: 'err',
        text: suspect != null
          ? `Landmarks disagree by ${rms!.toFixed(4)} of the plan. Landmark ${suspect + 1} looks misplaced.`
          : `Landmarks disagree by ${rms!.toFixed(4)} of the plan (limit ${MAX_RMS}). Recheck the pins.`,
      };
    }
    if (suspect != null) {
      return { tone: 'warn', text: `Good fit, but landmark ${suspect + 1} is pulling against the others.` };
    }
    return { tone: 'ok', text: 'Landmarks agree. Save to store this camera’s coverage.' };
  })();

  const statusColor = status.tone === 'err' ? 'var(--danger, #d9534f)'
    : status.tone === 'warn' ? 'var(--warn, #e0a030)'
      : status.tone === 'ok' ? 'var(--ok, #4caf7d)'
        : status.tone === 'accent' ? 'var(--accent)' : 'var(--dim)';

  const isPtz = camera?.ptz_capable === true;

  return (
    <Modal
      open={open}
      width="min(1180px, 96vw)"
      title={`Register coverage — ${camera?.name || camera?.slug || ''}`}
      onClose={onClose}
      footer={
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button className="btn-primary btn-sm" disabled={!canSave}
                  onClick={() => fitted && onSave(pairs, fitted)}>
            {busy ? <span className="spinner" /> : existing ? 'Save changes' : 'Save registration'}
          </button>
          <button className="btn-subtle btn-sm" onClick={onClose}>Cancel</button>
          {pairs.length > 0 && (
            <button className="btn-subtle btn-sm" onClick={() => { setPairs([]); setPendingFrame(null); }}>
              Clear landmarks
            </button>
          )}
          {existing && (
            <button className="btn-danger btn-sm" style={{ marginLeft: 'auto' }} disabled={busy}
                    onClick={() => onDelete(existing.id)}>
              Remove registration
            </button>
          )}
        </div>
      }
    >
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12 }}>
        Match the same real-world spot in both images, {MIN_PAIRS} times or more.
        Pick things at <strong>ground level</strong> — pole bases, fence-post feet, painted
        marks, drain covers. A roof corner or the top of a stack fits the maths and
        then places every zone metres away from where you meant.
      </div>

      {isPtz && (
        <div style={{
          fontSize: 12, marginBottom: 12, padding: '8px 10px', borderRadius: 6,
          background: 'var(--warn-bg, rgba(224,160,48,.12))', color: 'var(--warn, #e0a030)',
        }}>
          This camera can move. What you register here is the view it is
          <strong> pointing at right now</strong> — if it is moved afterwards, the coverage
          shown on the map no longer matches what it sees. Per-preset registration is
          coming; until then, register a camera that stays put.
        </div>
      )}

      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
        <PickPanel
          label="Camera view"
          hint={pendingFrame ? 'waiting on the plan…' : 'click a landmark'}
          src={frameUrl}
          pins={pairs.map(p => ({ x: p.fx, y: p.fy }))}
          pending={pendingFrame}
          suspect={suspect}
          armed={!pendingFrame}
          onPick={onFramePick}
          onRemove={removePair}
        />
        <PickPanel
          label={`Site plan${plan ? ` — ${plan.name}` : ''}`}
          hint={pendingFrame ? 'click the matching spot' : 'start in the camera view'}
          src={planImgUrl}
          pins={pairs.map(p => ({ x: p.mx, y: p.my }))}
          pending={null}
          suspect={suspect}
          armed={!!pendingFrame}
          onPick={onPlanPick}
          onRemove={removePair}
        >
          {preview.length >= 3 && (
            // viewBox 0 0 1 1 with preserveAspectRatio="none" maps normalized
            // plan coordinates straight onto the rendered image, and the
            // viewBox clips anything running off the plan — the same area the
            // server drops when it stores the footprint.
            <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true"
                 style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}>
              <polygon
                points={preview.map(([x, y]) => `${x},${y}`).join(' ')}
                fill="var(--accent)" fillOpacity={0.16}
                stroke="var(--accent)" strokeWidth={0.004} vectorEffect="non-scaling-stroke"
              />
            </svg>
          )}
        </PickPanel>
      </div>

      <div style={{
        display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
        marginTop: 14, paddingTop: 12, borderTop: '1px solid var(--line, #2a2f38)',
      }}>
        <span style={{ fontSize: 12.5, fontWeight: 650 }}>
          {pairs.length} landmark{pairs.length === 1 ? '' : 's'}
        </span>
        {rms != null && Number.isFinite(rms) && (
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            error {rms.toFixed(4)} of the plan
            {scaleM != null && <> · about {(rms * scaleM).toFixed(2)} m</>}
            {scaleM == null && plan && !plan.calibration && (
              <span style={{ color: 'var(--dim)' }}> · georeference the plan to see this in metres</span>
            )}
          </span>
        )}
        <span style={{ fontSize: 12, color: statusColor, flex: '1 1 260px' }}>{status.text}</span>
      </div>
    </Modal>
  );
}
