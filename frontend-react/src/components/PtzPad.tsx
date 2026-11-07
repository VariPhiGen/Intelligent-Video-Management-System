/**
 * PtzPad.tsx — the PTZ control surface, shared by the live expanded view and
 * the per-camera config tab.
 *
 * Everything here is gated on what the CAMERA said it can do, never on what the
 * control could theoretically send. A dome that advertises continuous pan/tilt
 * but no continuous zoom gets arrows and no zoom rocker; one that reports no
 * relative space gets no step buttons and no click-to-centre. Offering a
 * control the camera will answer with a SOAP fault is worse than not offering
 * it — the operator cannot tell a broken product from a broken camera.
 *
 * Interaction model, matching what every major VMS does:
 *   • press-and-hold an arrow → continuous move, stops on release
 *   • the pad hub → an explicit Stop, always live even when nothing is moving
 *     (it is the control someone reaches for when a camera is running away)
 *   • arrow keys / +- while the pad has focus → the same continuous move, so
 *     the feature is usable without a mouse (public-sector accessibility asks
 *     for this by name)
 *   • press an arrow and DRAG → steer: the direction follows the pointer around
 *     the hub and the speed follows its distance from it (toward the hub =
 *     slower), like a joystick (steerVector in lib/ptz.ts)
 *
 * Zoom-aware (2026-10-27): on a calibrated camera the pad shows the magnification
 * ("3.2×"), offers 1× / 2× / 4× … / Max, and divides pan/tilt speed by the
 * magnification — at 12× an undivided arrow throws the scene off-screen before
 * anyone can let go (zoomedSpeed). Uncalibrated cameras keep plain +/−.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  PtzDriver, can, magLabel, percentSteps, ptzApi, steerVector, zoomPercent, zoomRange, zoomedSpeed,
  type Axis, type ZoomScale,
} from '@/lib/ptz';
import type { PtzCapabilities } from '@/lib/types';

/** Eight compass directions as (pan, tilt) unit vectors, ONVIF-signed:
 *  pan +1 = right, tilt +1 = UP. Diagonals are normalised so a corner press is
 *  the same speed as a straight one rather than 1.41× faster. */
const D = 0.7071;
const DIRS: { key: string; label: string; pan: number; tilt: number; area: string }[] = [
  { key: 'nw', label: '↖', pan: -D, tilt: D, area: 'nw' },
  { key: 'n', label: '▲', pan: 0, tilt: 1, area: 'n' },
  { key: 'ne', label: '↗', pan: D, tilt: D, area: 'ne' },
  { key: 'w', label: '◀', pan: -1, tilt: 0, area: 'w' },
  { key: 'e', label: '▶', pan: 1, tilt: 0, area: 'e' },
  { key: 'sw', label: '↙', pan: -D, tilt: -D, area: 'sw' },
  { key: 's', label: '▼', pan: 0, tilt: -1, area: 's' },
  { key: 'se', label: '↘', pan: D, tilt: -D, area: 'se' },
];

const KEYS: Record<string, Axis> = {
  ArrowUp: { tilt: 1 },
  ArrowDown: { tilt: -1 },
  ArrowLeft: { pan: -1 },
  ArrowRight: { pan: 1 },
  '+': { zoom: 1 },
  '=': { zoom: 1 },
  '-': { zoom: -1 },
  _: { zoom: -1 },
};

const ZOOM_POLL_MS = 3000;
const DRAG_START_PX = 6;          // a press that wanders less than this is a plain press, not a steer

/** The calibrated zoom scale, refreshed while the pad is on screen (one GetStatus per poll, skipped while the tab
 *  is hidden). `refresh` after a zoom move so the readout catches up without waiting for the next poll. */
function useZoomScale(cameraId: string, enabled: boolean):
    { scale: ZoomScale | null; zoomPos: number | null; refresh: () => void } {
  const [scale, setScale] = useState<ZoomScale | null>(null);
  const [zoomPos, setZoomPos] = useState<number | null>(null);
  const aliveRef = useRef(true);
  const load = useCallback(() => {
    if (!enabled || document.hidden) return;
    ptzApi.status(cameraId)
      .then(r => {
        if (!aliveRef.current) return;
        setScale(r.zoom_scale ?? null);
        setZoomPos(r.status?.zoom ?? null);
      })
      .catch(() => { /* advisory — the pad works without it */ });
  }, [cameraId, enabled]);
  useEffect(() => {
    aliveRef.current = true;
    setScale(null);
    if (!enabled) return;
    load();
    const t = setInterval(load, ZOOM_POLL_MS);
    return () => { aliveRef.current = false; clearInterval(t); };
  }, [load, enabled]);
  return { scale, zoomPos, refresh: load };
}

export function PtzPad({ cameraId, caps, speed, onError, compact }: {
  cameraId: string;
  caps: PtzCapabilities;
  /** 0..1 — scales every continuous velocity and every relative step. */
  speed: number;
  onError: (msg: string) => void;
  compact?: boolean;
}) {
  const [active, setActive] = useState<string | null>(null);
  const driverRef = useRef<PtzDriver | null>(null);

  const panTilt = can(caps, 'continuous', 'pan_tilt');
  const zoom = can(caps, 'continuous', 'zoom');
  const zoomTo = can(caps, 'absolute', 'zoom');
  const { scale, zoomPos, refresh } = useZoomScale(cameraId, zoom || zoomTo);
  const mag = scale?.mag ?? null;
  const range = zoomRange(caps);
  const pct = mag ? null : zoomPercent(zoomPos, range);
  // The chips: measured magnifications on a calibrated camera, else percent of the range — either way only when
  // the camera can go straight to a zoom position.
  const chips: { label: string; zoom: number; title: string; on: boolean }[] = !zoomTo ? []
    : scale && scale.steps.length > 1
      ? scale.steps.map(st => {
          const est = st.mag > scale.measured_to * 1.05 ? '≈' : '';
          return {
            label: st.max ? `Max ${est}${magLabel(st.mag)}` : `${est}${magLabel(st.mag)}`, zoom: st.zoom,
            title: est ? 'Estimated — calibration could not measure this far' : `Zoom to ${magLabel(st.mag)}`,
            on: !!mag && Math.abs(Math.log(mag / st.mag)) < 0.12,
          };
        })
      : range ? percentSteps(range).map(st => ({
          ...st, title: `${st.title} — calibrate this camera to see real ×`,
          on: zoomPos != null && Math.abs(zoomPos - st.zoom) <= (range[1] - range[0]) * 0.03,
        }))
      : [];
  const ptSpeed = zoomedSpeed(speed, mag);
  const padRef = useRef<HTMLDivElement | null>(null);
  const downRef = useRef<{ x: number; y: number; steering: boolean } | null>(null);

  if (driverRef.current === null) {
    driverRef.current = new PtzDriver(cameraId, onError);
  }

  const begin = useCallback((key: string, axis: Axis) => {
    setActive(key);
    driverRef.current?.start({
      pan: axis.pan !== undefined ? axis.pan * ptSpeed : undefined,
      tilt: axis.tilt !== undefined ? axis.tilt * ptSpeed : undefined,
      zoom: axis.zoom !== undefined ? axis.zoom * speed : undefined,
    });
  }, [speed, ptSpeed]);

  /** Drag-steering: once a held arrow's pointer has wandered, the move follows it (see steerVector). */
  function steer(e: React.PointerEvent) {
    const d = downRef.current;
    const pad = padRef.current;
    if (!d || !pad) return;
    if (!d.steering && Math.hypot(e.clientX - d.x, e.clientY - d.y) < DRAG_START_PX) return;
    d.steering = true;
    const r = pad.getBoundingClientRect();
    const v = steerVector(e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2), r.width / 2);
    if (!v) return;                     // over the hub: keep the last direction rather than jitter to a stop
    driverRef.current?.steer({ pan: v.pan * ptSpeed, tilt: v.tilt * ptSpeed });
  }

  function goZoom(z: number) {
    ptzApi.absolute(cameraId, { zoom: z, speed: Math.max(speed, 0.5) })
      .then(() => { setTimeout(refresh, 1200); setTimeout(refresh, 3500); })
      .catch((e: any) => onError(e?.message || 'Zoom failed'));
  }

  const end = useCallback(() => {
    setActive(null);
    void driverRef.current?.stop();
  }, []);

  // Every way a held control can end WITHOUT a pointerup on the button: the tab
  // is hidden (laptop lid, app switch), the window loses focus, or the
  // component unmounts because the operator navigated away mid-drag. Each of
  // these has left a camera panning in some VMS; none of them should here.
  useEffect(() => {
    const driver = driverRef.current;
    const bail = () => { setActive(null); void driver?.stop(); };
    const onVisibility = () => { if (document.hidden) bail(); };
    window.addEventListener('blur', bail);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('blur', bail);
      document.removeEventListener('visibilitychange', onVisibility);
      void driver?.stop();
    };
  }, []);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.repeat) return;               // the heartbeat drives repeats, not the OS
    if (e.key === 'Escape') { end(); return; }
    const axis = KEYS[e.key];
    if (!axis) return;
    if (axis.zoom !== undefined ? !zoom : !panTilt) return;
    e.preventDefault();
    begin(`key:${e.key}`, axis);
  }

  function onKeyUp(e: React.KeyboardEvent) {
    if (active === `key:${e.key}`) end();
  }

  const size = compact ? 108 : 148;

  const press = (key: string, axis: Axis, steerable = false) => ({
    onPointerDown: (e: React.PointerEvent) => {
      e.preventDefault();
      // Capture keeps the drag bound to this button when the cursor slides off
      // it — which it will, the moment someone swings a camera hard left.
      (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
      downRef.current = steerable ? { x: e.clientX, y: e.clientY, steering: false } : null;
      begin(key, axis);
    },
    onPointerMove: steerable ? steer : undefined,
    onPointerUp: () => { downRef.current = null; end(); },
    onPointerCancel: () => { downRef.current = null; end(); },
    onLostPointerCapture: () => { downRef.current = null; end(); },
  });

  return (
    <div className="ptz-ctl">
      <div className="ptz-ctl-row">
        <div
          ref={padRef}
          className={`ptz-pad${panTilt ? '' : ' ptz-pad-off'}`}
          style={{ width: size, height: size, minWidth: size }}
          title={mag && mag > 1.5 ? `Moves slowed for ${magLabel(mag)} zoom · drag from an arrow to steer`
                                  : 'Hold an arrow to move · drag from it to steer'}
          tabIndex={panTilt ? 0 : -1}
          role="group"
          aria-label="Pan and tilt. Hold a direction, or use the arrow keys."
          onKeyDown={onKeyDown}
          onKeyUp={onKeyUp}
          onBlur={end}
        >
          {DIRS.map(d => (
            <button
              key={d.key}
              type="button"
              className={`ptz-dir ptz-${d.area}${active === d.key ? ' on' : ''}`}
              disabled={!panTilt}
              aria-label={d.key}
              {...press(d.key, { pan: d.pan, tilt: d.tilt }, true)}
            >
              {d.label}
            </button>
          ))}
          {/* Always enabled, even with nothing moving and even on a camera whose
              pan/tilt we could not confirm: this is the panic control. */}
          <button
            type="button"
            className="ptz-hub"
            title="Stop all movement"
            aria-label="Stop"
            onClick={() => { setActive(null); void ptzApi.stop(cameraId).catch(() => {}); }}
          >
            ■
          </button>
        </div>

        {(zoom || chips.length > 0) && (
          <div className={`ptz-zoom${compact ? ' compact' : ''}`} role="group" aria-label="Zoom">
            {zoom && (
              <button type="button" className={`ptz-z${active === 'tele' ? ' on' : ''}`}
                      title="Zoom in (tele) — hold" aria-label="Zoom in" {...press('tele', { zoom: 1 })}>＋</button>
            )}
            <span className={`ptz-z-label${mag || pct != null ? ' mag' : ''}`}
                  title={mag ? 'Magnification, measured by calibration'
                    : pct != null ? 'Percent of the zoom range (calibrate for real ×)' : undefined}>
              {mag ? `${scale && mag > scale.measured_to * 1.05 ? '≈' : ''}${magLabel(mag)}`
                : pct != null ? `${pct}%` : 'ZOOM'}
            </span>
            {zoom && (
              <button type="button" className={`ptz-z${active === 'wide' ? ' on' : ''}`}
                      title="Zoom out (wide) — hold" aria-label="Zoom out" {...press('wide', { zoom: -1 })}>－</button>
            )}
          </div>
        )}
      </div>

      {chips.length > 0 && (
        <div className="ptz-zchips" role="group" aria-label="Zoom to">
          {chips.map(c => (
            <button key={c.label} type="button" className={`ptz-zchip${c.on ? ' on' : ''}`} title={c.title}
                    onClick={() => goZoom(c.zoom)}>
              {c.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** Speed applies to continuous velocity AND relative step size, so one control
 *  governs both and an operator never has to reason about which is in play. */
export function PtzSpeed({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  return (
    <label className="ptz-speed">
      Speed
      <input type="range" min={0.1} max={1} step={0.05} value={value}
             onChange={e => onChange(Number(e.target.value))}
             />
      <span className="ptz-speed-val">
        {Math.round(value * 100)}%
      </span>
    </label>
  );
}
