/**
 * MapMarkers.tsx — everything drawn INSIDE the plan's transformed layer:
 * camera dots with their name labels, calibration pins, and zone rectangles.
 *
 * One component for both surfaces that draw dots: the Cameras layer (which can
 * drag them in edit mode) and the Heatmap layer (which cannot). They were two
 * near-identical inline blocks in MapPage, so a change to dot styling had to be
 * made twice and drifted between them.
 *
 * Pass `drag` to get the editable behaviour; omit it for a read-only layer.
 * Positioning is percentage-based inside the transformed layer, and each dot
 * counter-scales by 1/scale so it stays a constant screen size while zooming.
 */
import { Fragment } from 'react';

import { zoneOf } from '@/lib/cameras';
import type { Camera } from '@/lib/types';

import { dotColor, type ResolvedDot, type ZoneBox } from './mapHelpers';

export interface DotDragHandlers {
  /** Live position of the dot under the pointer, if any. */
  dragPos: { id: string; x: number; y: number } | null;
  /** Post-drop, pre-refresh positions, so a dot doesn't flicker back to stale
   *  data while the PUT and the camera refresh are in flight. */
  optimistic: Record<string, { x: number; y: number }>;
  onPointerDown: (e: React.PointerEvent, cam: Camera) => void;
  onPointerMove: (e: React.PointerEvent, cam: Camera) => void;
  onPointerUp: (e: React.PointerEvent, cam: Camera) => void;
  /** Fired for both pointercancel and lostpointercapture. */
  onPointerAbort: (cam: Camera) => void;
}

export function CameraDots({
  dots, motion, scale, labels, selectedId, onSelect, drag,
}: {
  dots: ResolvedDot[];
  motion: Record<string, string | undefined>;
  scale: number;
  labels: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  drag?: DotDragHandlers;
}) {
  return (
    <>
      {dots.map(d => {
        const color = dotColor(d.cam, motion[d.cam.slug]);
        const triggered = motion[d.cam.slug] === 'TRIGGERED';
        const isDragging = drag?.dragPos?.id === d.cam.id;
        const opt = drag?.optimistic[d.cam.id];
        // Priority: live drag position > optimistic override > real data.
        const px = isDragging ? drag!.dragPos!.x : (opt ? opt.x : d.x);
        const py = isDragging ? drag!.dragPos!.y : (opt ? opt.y : d.y);
        return (
          <Fragment key={d.cam.id}>
            {labels && (
              <span style={{
                position: 'absolute', left: `${px * 100}%`, top: `${py * 100}%`,
                transform: `translate(11px, -50%) scale(${1 / scale})`, transformOrigin: '0 50%',
                pointerEvents: 'none', whiteSpace: 'nowrap',
                fontSize: 11, fontWeight: 600, lineHeight: 1.5,
                color: 'var(--text)', background: 'var(--surface)',
                border: '1px solid var(--border)', borderRadius: 4, padding: '0 5px',
                boxShadow: '0 1px 3px rgba(0,0,0,.18)',
                opacity: d.cam.id === selectedId ? 1 : .92,
              }}>{d.cam.name}</span>
            )}
            <button
              title={`${d.cam.name} · ${zoneOf(d.cam)} · ${d.cam.health_status}${d.auto ? ' · auto-placed from GPS' : ''}`}
              onMouseDown={e => e.stopPropagation()}
              onClick={e => { e.stopPropagation(); onSelect(d.cam.id); }}
              {...(drag ? {
                onPointerDown: (e: React.PointerEvent) => drag.onPointerDown(e, d.cam),
                onPointerMove: (e: React.PointerEvent) => drag.onPointerMove(e, d.cam),
                onPointerUp: (e: React.PointerEvent) => drag.onPointerUp(e, d.cam),
                onPointerCancel: () => drag.onPointerAbort(d.cam),
                onLostPointerCapture: () => drag.onPointerAbort(d.cam),
              } : {})}
              style={{
                position: 'absolute', left: `${px * 100}%`, top: `${py * 100}%`,
                transform: `translate(-50%,-50%) scale(${1 / scale})`,
                width: 14, height: 14, borderRadius: '50%',
                border: d.cam.id === selectedId ? '2px solid var(--accent)' : 'none',
                cursor: drag ? (isDragging ? 'grabbing' : 'grab') : 'pointer',
                padding: 0,
                touchAction: drag ? 'none' : undefined,
                background: color,
                // The global `pulse` keyframe rings with currentColor (same
                // mechanism as .badge-live .badge-dot) — set `color` so a
                // TRIGGERED dot pulses in its own orange.
                color,
                ...(triggered
                  ? { animation: 'pulse 2s var(--ease) infinite' }
                  // Auto (GPS-derived) dots get a white halo so they're
                  // visibly distinct from hand-placed ones.
                  : { boxShadow: d.auto
                      ? '0 0 0 2px rgba(0,0,0,.25), 0 0 0 4px rgba(255,255,255,.5)'
                      : '0 0 0 2px rgba(0,0,0,.25)' }),
              }} />
          </Fragment>
        );
      })}
    </>
  );
}

/** The numbered control points dropped while georeferencing a plan. */
export function CalibrationPins({ points, scale }: {
  points: { x: number; y: number }[];
  scale: number;
}) {
  return (
    <>
      {points.map((p, i) => (
        <div key={`cal-${i}`} style={{
          position: 'absolute', left: `${p.x * 100}%`, top: `${p.y * 100}%`,
          transform: `translate(-50%,-50%) scale(${1 / scale})`,
          width: 22, height: 22, borderRadius: '50%',
          background: 'var(--accent)', color: '#fff',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontSize: 11, fontWeight: 700, border: '2px solid #fff',
          boxShadow: '0 1px 5px rgba(0,0,0,.45)', pointerEvents: 'none',
        }}>{i + 1}</div>
      ))}
    </>
  );
}

/** Computed zone rectangles — shared by the Cameras, HA Devices and Heatmap
 * layers so all three agree on where a zone is. */
export function ZoneBoxes({ boxes }: { boxes: ZoneBox[] }) {
  return (
    <>
      {boxes.map(b => (
        <div key={b.zone} style={{
          position: 'absolute', left: `${b.x * 100}%`, top: `${b.y * 100}%`,
          width: `${b.w * 100}%`, height: `${b.h * 100}%`,
          border: '1px solid var(--border2)', borderRadius: 8, pointerEvents: 'none',
        }}>
          <span style={{
            position: 'absolute', top: 6, left: 8, fontSize: 10,
            letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--dim)',
          }}>
            {b.zone}
          </span>
        </div>
      ))}
    </>
  );
}

/**
 * Coverage footprints — the ground each registered camera actually sees.
 *
 * Drawn as one SVG beneath the camera dots, in the same normalized plan
 * coordinates everything else on this map uses (viewBox 0 0 1 1 with
 * preserveAspectRatio="none"), so it needs no knowledge of the plan's pixel
 * size or the current zoom.
 *
 * The polygons come from the server, which clipped them to the plan and dropped
 * everything past each camera's horizon. That matters for what an operator can
 * conclude: an area with no polygon over it is genuinely unwatched by any
 * registered camera, rather than merely beyond where the drawing stopped.
 *
 * A camera registered but not covering anything on this plan arrives with an
 * empty footprint and is skipped — there is no shape to draw, and a dot with no
 * polygon is the honest picture.
 */
export function CoverageFootprints({ footprints, selectedId }: {
  footprints: { cameraId: string; points: number[][] }[];
  selectedId: string | null;
}) {
  const usable = footprints.filter(f => f.points.length >= 3);
  if (!usable.length) return null;
  return (
    <svg
      viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true"
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
    >
      {usable.map(({ cameraId, points }) => {
        // The selected camera is lifted rather than colour-coded: every
        // footprint means the same thing, so the only distinction worth drawing
        // is which one the operator is currently looking at.
        const selected = cameraId === selectedId;
        return (
          <polygon
            key={cameraId}
            points={points.map(([x, y]) => `${x},${y}`).join(' ')}
            fill="var(--accent)"
            fillOpacity={selected ? 0.3 : 0.12}
            stroke="var(--accent)"
            strokeOpacity={selected ? 0.95 : 0.45}
            strokeWidth={selected ? 2 : 1}
            vectorEffect="non-scaling-stroke"
          />
        );
      })}
    </svg>
  );
}
