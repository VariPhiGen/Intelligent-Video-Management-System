/**
 * PtzControl.tsx — the PTZ block in the live expanded view's info panel.
 *
 * Replaces the decorative pad that used to sit here behind a "soon" badge. It
 * renders only three ways, and which one you get is decided by the CAMERA, not
 * by this component:
 *
 *   • a working pad, when the device advertises ONVIF PTZ and the operator has
 *     the `ptz_control` capability;
 *   • one short sentence saying exactly why not, for the cases an operator can
 *     do something about (no credentials → where to add them; no permission →
 *     whose problem it is);
 *   • nothing at all on a fixed camera, which is the common case. A greyed-out
 *     pad on a camera that can never pan is noise on every single fixed camera
 *     in the fleet, forever.
 */
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '@/lib/auth';
import { useToast } from '@/components/Toast';
import { PtzPad, PtzSpeed } from '@/components/PtzPad';
import {
  accumulatedZoomStep, boxToCentre, can, framePoint, grabFrame, pictureRect, ptzApi, usePtzCapabilities, waitForSettle,
} from '@/lib/ptz';
import type { Camera, PtzPreset } from '@/lib/types';

export function PtzControl({ cam, sectionStyle }: {
  cam: Camera;
  sectionStyle: React.CSSProperties;
}) {
  const nav = useNavigate();
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  const canPtz = isAdmin || me?.permissions?.ptz_control === true;
  const [speed, setSpeed] = useState(0.6);
  const [presets, setPresets] = useState<PtzPreset[] | null>(null);

  // Self-healing capability check: a camera never probed (ptz === null) is
  // probed HERE, through GET /ptz, the first time anyone expands it — because
  // discovery-onboarded cameras store credentials without ever passing the
  // one endpoint that used to probe, and would otherwise read "not checked
  // yet" forever. One SOAP round trip per camera ever; the answer persists.
  const { caps, checking, failed } = usePtzCapabilities(cam);

  const live = caps?.supported === true && canPtz;

  // Presets are a camera round trip, so only ask when the pad is actually
  // usable — and never block the pad on the answer.
  useEffect(() => {
    if (!live) { setPresets(null); return; }
    let alive = true;
    ptzApi.presets(cam.id)
      .then(r => { if (alive) setPresets(r.presets || []); })
      .catch(() => { if (alive) setPresets([]); });
    // Warm the backend's ONVIF session while the operator is still reaching
    // for the pad. The FIRST command on a camera otherwise pays the session
    // build (clock sync + GetCapabilities + WSDL parse, 1–3 s on real
    // hardware) and the pool idles out after ~2 min, so without this the
    // first press after any pause visibly lags and every later press doesn't
    // — which reads as flaky, not slow. GetStatus is the cheapest call that
    // builds it; the config tab gets the same effect from its status poll.
    ptzApi.status(cam.id).catch(() => { /* warm-up only — outcome irrelevant */ });
    return () => { alive = false; };
  }, [cam.id, live]);

  // A fixed camera says nothing. Every other "no" is actionable, so it speaks.
  if (!live) {
    if (caps && !caps.supported) return null;  // asked, and it is a fixed camera
    let reason: React.ReactNode;
    if (!canPtz) {
      reason = 'Your role does not include PTZ control.';
    } else if (!cam.onvif_capable) {
      reason = (
        <>
          No ONVIF credentials stored for this camera — add them under{' '}
          <a href="#" onClick={e => { e.preventDefault(); nav(`/cameras/config?cam=${cam.id}&tab=device`); }}>
            Device settings
          </a>{' '}and PTZ is detected automatically.
        </>
      );
    } else if (checking) {
      reason = 'Checking PTZ support on this camera…';
    } else if (failed) {
      reason = 'Could not reach the camera to check PTZ support — it retries next time this view opens.';
    } else {
      return null;  // caps arrived unsupported between renders
    }
    return (
      <>
        <div style={sectionStyle}>PTZ control</div>
        <div style={{ fontSize: 11.5, color: 'var(--dim)', marginBottom: 18 }}>{reason}</div>
      </>
    );
  }

  return (
    <>
      <div style={sectionStyle}>PTZ control</div>
      <div style={{ marginBottom: 18, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <PtzPad cameraId={cam.id} caps={caps!} speed={speed} compact
                onError={m => toast(m, 'err')} />
        <PtzSpeed value={speed} onChange={setSpeed} />

        {presets !== null && presets.length > 0 && (
          <div className="ptz-presets">
            {presets.slice(0, 8).map(p => (
              <button key={p.token} type="button" className="ptz-preset"
                      title={`Go to ${p.name || p.token}`}
                      onClick={() => ptzApi.gotoPreset(cam.id, p.token, speed)
                        .then(() => toast(`Moving to ${p.name || p.token}`))
                        .catch((e: any) => toast(e.message, 'err'))}>
                {p.name || p.token}
              </button>
            ))}
          </div>
        )}

        {caps?.home?.supported && (
          <button className="btn-ghost btn-sm"
                  onClick={() => ptzApi.gotoHome(cam.id, speed)
                    .then(() => toast('Returning to home position'))
                    .catch((e: any) => toast(e.message, 'err'))}>
            ⌂ Home position
          </button>
        )}

        <div style={{ fontSize: 10.5, color: 'var(--dim)' }}>
          Hold a direction to move; release to stop. Arrow keys work when the pad
          has focus. Presets and home are managed in{' '}
          <a href="#" onClick={e => { e.preventDefault(); nav(`/cameras/config?cam=${cam.id}&tab=ptz`); }}>
            Configuration → PTZ
          </a>.
        </div>
      </div>
    </>
  );
}


/**
 * PtzClickLayer — click-to-centre, drag-to-zoom, right-click zoom-out and wheel zoom over the live picture.
 *
 * WHERE: the layer covers the whole video panel, but the picture inside it is letterboxed (`object-fit:
 * contain`), so every point is measured against the PICTURE's rectangle (pictureRect in lib/ptz.ts) and clicks
 * on the black bars are ignored. Measuring against the panel was half of "it never makes the click the centre"
 * (KEC kec01, 2026-10-26).
 *
 * CHECKED: a plain click also sends the frame it was made on (grabFrame); once the head stops, the server measures
 * where the clicked spot actually went and corrects once if it missed (alpha i-PRO, 2026-10-27).
 *
 * HOW FAR: the layer only sends WHERE in the picture (x, y in -1..1, plus a zoom factor for a drawn box) to
 * POST /ptz/centre; the server turns that into the move from the camera's measured field of view at its
 * current zoom (services/ptz_calibrate). The old client-side fixed fraction of the camera's WHOLE pan range was
 * the other half: right only at one zoom, tens of degrees off zoomed in. A camera nobody has calibrated still
 * gets the old fixed nudge server-side, and the layer says once that Calibrate (PTZ settings) makes it exact.
 *
 * Click vs. drag is raw screen distance at release (DRAG_THRESHOLD_PX). A drawn box zooms so its larger side
 * fills the frame; right-click zooms out by half; the wheel stays a relative zoom step (no point to centre on).
 */
const DRAG_THRESHOLD_PX = 10;
const DEAD_ZONE = 0.03;            // a click this close to centre is a mis-click, not a request to nudge
const RIGHT_CLICK_ZOOM_OUT = 0.5;  // half the magnification

// One command at a time: a second click while the camera is still travelling used to stack a second move on
// the first. The lock holds until waitForSettle (lib/ptz.ts) sees GetStatus stop reporting MOVING.

// A wheel gesture fires many small events; accumulate them and send ONE relative zoom step once it pauses.
const ZOOM_DEBOUNCE_MS = 150;
const ZOOM_STEP_SCALE = 0.0025;   // deltaY units -> ONVIF relative-zoom fraction
const ZOOM_STEP_MAX = 0.3;        // clamp so a fast fling can't fling the zoom motor just as hard

// "Calibrate for exact centring" is said once per camera per page load, not on every click.
const calibrationHinted = new Set<string>();

function inside(x: number, y: number, r: { left: number; top: number; width: number; height: number }): boolean {
  return x >= r.left && x <= r.left + r.width && y >= r.top && y <= r.top + r.height;
}

export function PtzClickLayer({ cam, speed = 0.6 }: { cam: Camera; speed?: number }) {
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  const canPtz = isAdmin || me?.permissions?.ptz_control === true;
  // Shares PtzControl's check through the module-level dedupe map, so an
  // expanded camera costs one probe even with both consumers mounted.
  const { caps } = usePtzCapabilities(cam);

  const canCentre = can(caps, 'relative', 'pan_tilt') || can(caps, 'absolute', 'pan_tilt');
  const canZoom = can(caps, 'relative', 'zoom') || can(caps, 'absolute', 'zoom');
  const canWheel = can(caps, 'relative', 'zoom');       // the wheel sends a relative zoom step

  // Refs, not state, for the values read inside the debounce timer, the
  // waitForSettle poll loop, and the pointermove/pointerup handlers: all of
  // these close over whatever was current when they started or were last
  // scheduled, and state from that render would be stale by the time they run.
  const [busy, setBusy] = useState(false);
  const [marker, setMarker] = useState<{ x: number; y: number } | null>(null);
  const [dragBox, setDragBox] = useState<{ left: number; top: number; width: number; height: number } | null>(null);
  const busyRef = useRef(false);
  const zoomAccumRef = useRef(0);
  const zoomTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The rect is captured once at pointerdown, not re-queried on every
  // pointermove — the video panel doesn't resize mid-drag, and re-querying
  // per move event is needless layout work on what can be a fast-firing event.
  const dragRef = useRef<{ x0: number; y0: number; rect: DOMRect; pic: { left: number; top: number; width: number; height: number } } | null>(null);
  // waitForSettle has no built-in cancellation (it's a plain poll loop), so a
  // click fired just before the operator navigates away leaves it polling for
  // up to SETTLE_MAX_WAIT_MS after this component is gone — mountedRef is the
  // guard so that resolution's setState is a no-op instead of the "update on
  // an unmounted component" warning that trains everyone to ignore real ones
  // (same reasoning as PtzPad's blur/visibility cleanup).
  const mountedRef = useRef(true);

  useEffect(() => () => {
    mountedRef.current = false;
    if (zoomTimerRef.current) clearTimeout(zoomTimerRef.current);
  }, []);

  if (!canPtz || !caps?.supported || (!canCentre && !canZoom)) return null;

  function boxStyle(x0: number, y0: number, x1: number, y1: number, rect: { left: number; top: number }) {
    return {
      left: Math.min(x0, x1) - rect.left, top: Math.min(y0, y1) - rect.top,
      width: Math.abs(x1 - x0), height: Math.abs(y1 - y0),
    };
  }

  // Pointer events, not mouse events — matches PtzPad/PtzDriver (see lib/ptz.ts's
  // module docstring): pointer capture keeps the gesture bound to this layer
  // even if the cursor slides off it mid-drag, which a fast drag toward a
  // frame edge will do constantly.
  function onPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if (!canCentre || busyRef.current) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pic = pictureRect(e.currentTarget);
    if (!inside(e.clientX, e.clientY, pic)) return;        // on the black bars: not a point in the picture
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    dragRef.current = { x0: e.clientX, y0: e.clientY, rect, pic };
    setDragBox(boxStyle(e.clientX, e.clientY, e.clientX, e.clientY, rect));
  }

  function onPointerMove(e: React.PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    if (!d) return;
    setDragBox(boxStyle(d.x0, d.y0, e.clientX, e.clientY, d.rect));
  }

  function onPointerUp(e: React.PointerEvent<HTMLDivElement>) {
    const d = dragRef.current;
    dragRef.current = null;
    setDragBox(null);
    // A gesture that started this drag can still have gone busy in the
    // meantime (e.g. a concurrent wheel-zoom — see onWheel's dragRef guard for
    // the other half of that race) — cancel rather than stack a second command.
    if (!d || busyRef.current) return;

    const dragPx = Math.hypot(e.clientX - d.x0, e.clientY - d.y0);
    let body: { x: number; y: number; zoom_factor?: number } | null = null;
    if (canZoom && dragPx >= DRAG_THRESHOLD_PX) {
      // A sliver box (boxToCentre's own guard) falls through to centring on the release point, like a click.
      body = boxToCentre(d.x0, d.y0, e.clientX, e.clientY, d.pic);
    }
    if (!body) {
      const p = framePoint(e.clientX, e.clientY, d.pic);
      if (Math.abs(p.x) < DEAD_ZONE && Math.abs(p.y) < DEAD_ZONE) return;
      body = p;
    }
    // A plain click carries the picture it was made on, so the server can check where the spot landed and
    // correct once (services/ptz_calibrate.verify_centre). Not for a zoom: the picture changes scale.
    const frame = body.zoom_factor ? null : grabFrame(e.currentTarget);
    send(frame ? { ...body, frame } : body, { x: e.clientX - d.rect.left, y: e.clientY - d.rect.top },
         !!body.zoom_factor);
  }

  function send(body: { x: number; y: number; zoom_factor?: number; frame?: string },
                at: { x: number; y: number }, zooms: boolean) {
    busyRef.current = true;
    setBusy(true);
    setMarker(at);
    ptzApi.centre(cam.id, { ...body, speed })
      .then(r => {
        if (!r.calibrated && !calibrationHinted.has(cam.id)) {
          calibrationHinted.add(cam.id);
          toast('This camera is not calibrated yet, so centring is approximate — Cameras → Configure → PTZ → '
                + 'Calibrate makes clicks land exactly.');
        }
        return waitForSettle(cam.id, { panTilt: true, zoom: zooms });
      })
      .catch((err: any) => toast(err.message, 'err'))
      .finally(() => {
        if (!mountedRef.current) return;
        busyRef.current = false;
        setBusy(false);
        setMarker(null);
      });
  }

  function onPointerCancel() {
    dragRef.current = null;
    setDragBox(null);
  }

  function onContextMenu(e: React.MouseEvent<HTMLDivElement>) {
    e.preventDefault();   // replace the browser's menu — right-click is the zoom-out gesture
    if (busyRef.current || dragRef.current) return;
    const pic = pictureRect(e.currentTarget);
    if (!inside(e.clientX, e.clientY, pic)) return;
    const r = e.currentTarget.getBoundingClientRect();
    const p = canCentre ? framePoint(e.clientX, e.clientY, pic) : { x: 0, y: 0 };
    send({ ...p, zoom_factor: RIGHT_CLICK_ZOOM_OUT }, { x: e.clientX - r.left, y: e.clientY - r.top }, true);
  }

  function onWheel(e: React.WheelEvent<HTMLDivElement>) {
    if (!canWheel || dragRef.current) return;   // a drag in progress owns the gesture — see onPointerUp
    e.preventDefault();
    zoomAccumRef.current += e.deltaY;
    if (zoomTimerRef.current) clearTimeout(zoomTimerRef.current);
    zoomTimerRef.current = setTimeout(() => {
      const zoom = accumulatedZoomStep(zoomAccumRef.current, ZOOM_STEP_SCALE, ZOOM_STEP_MAX);
      zoomAccumRef.current = 0;
      if (zoom === 0) return;   // trackpad jitter under minStep — see accumulatedZoomStep
      ptzApi.step(cam.id, { zoom, speed }).catch((err: any) => toast(err.message, 'err'));
    }, ZOOM_DEBOUNCE_MS);
  }

  const title = canCentre && canZoom
      ? 'Click to centre, drag to zoom into an area, right-click to zoom out, or scroll to zoom'
    : canCentre ? 'Click to centre the camera on this point'
    : 'Scroll to zoom, or right-click to zoom out';   // canZoom must be true to render at all here

  return (
    <div
      className={`ptz-click-layer${canCentre ? '' : ' ptz-click-layer-zoom-only'}${busy ? ' busy' : ''}`}
      onPointerDown={canCentre ? onPointerDown : undefined}
      onPointerMove={canCentre ? onPointerMove : undefined}
      onPointerUp={canCentre ? onPointerUp : undefined}
      onPointerCancel={canCentre ? onPointerCancel : undefined}
      onLostPointerCapture={canCentre ? onPointerCancel : undefined}
      onWheel={canWheel ? onWheel : undefined}
      onContextMenu={canZoom ? onContextMenu : undefined}
      title={title}
    >
      {dragBox && <span className="ptz-drag-box" style={dragBox} />}
      {marker && <span className="ptz-click-marker" style={{ left: marker.x, top: marker.y }} />}
    </div>
  );
}
