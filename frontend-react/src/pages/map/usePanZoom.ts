/**
 * usePanZoom.ts — the Map surface's pan, zoom and coordinate maths.
 *
 * Everything here used to live inline in MapPage, tangled with placement and
 * data loading. It is self-contained: give it nothing, mount `attachSurface`
 * on the panning box and `imgRef` on the plan image, and it owns the transform.
 *
 * The two things worth knowing before touching it:
 *   • Every transform goes through `clampTf`, so the plan can never leave the
 *     viewport, and centres when it is smaller than the surface.
 *   • `panMoved` is how the page tells a pan from a click. A drag that moved
 *     must not place a camera where it happened to stop.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { DRAG_SLOP_PX } from './mapConstants';

export interface Transform { scale: number; tx: number; ty: number }

const MIN_SCALE = 0.5, MAX_SCALE = 6;

export function usePanZoom() {
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  const [tf, setTf] = useState<Transform>({ scale: 1, tx: 0, ty: 0 });
  const [panning, setPanning] = useState(false);
  const drag = useRef<{ x: number; y: number } | null>(null);
  // Where the pan started, and whether it travelled past the slop.
  const panOrigin = useRef<{ x: number; y: number } | null>(null);
  const panMoved = useRef(false);

  // The plan's own aspect, learned on load, so the surface hugs the image
  // instead of leaving a dead band under a wide floor plan.
  const [imgAspect, setImgAspect] = useState<number | null>(null);

  // ── Pan bounds ─────────────────────────────────────────────────────────────
  // The plan panned without limits: you could drag it clean off the surface and
  // carry on dragging (and clicking) over empty background with no map under
  // the cursor at all.
  const clampTf = useCallback((t: Transform): Transform => {
    const surface = surfaceRef.current, img = imgRef.current;
    if (!surface || !img?.clientWidth || !img.clientHeight) return t;
    const axis = (v: number, content: number, view: number) =>
      content <= view ? (view - content) / 2 : Math.min(0, Math.max(view - content, v));
    return {
      scale: t.scale,
      tx: axis(t.tx, img.clientWidth * t.scale, surface.clientWidth),
      ty: axis(t.ty, img.clientHeight * t.scale, surface.clientHeight),
    };
  }, []);

  const onImgLoad = useCallback((e: React.SyntheticEvent<HTMLImageElement>) => {
    const el = e.currentTarget;
    if (el.naturalWidth && el.naturalHeight) setImgAspect(el.naturalWidth / el.naturalHeight);
    // The aspect we just set re-lays-out the surface, so clamp on the next
    // frame — otherwise we'd measure the pre-layout box.
    requestAnimationFrame(() => setTf(t => clampTf(t)));
  }, [clampTf]);

  // Re-clamp on resize: shrinking the window can otherwise leave the plan
  // parked off-surface at a transform that was legal at the old size.
  useEffect(() => {
    const onResize = () => setTf(t => clampTf(t));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [clampTf]);

  // Zoom from the buttons anchors on the surface centre — the wheel anchors on
  // the pointer, which has no meaning when the click came from a button.
  const zoomBy = useCallback((k: number) => setTf(t => {
    const el = surfaceRef.current;
    const px = (el?.clientWidth ?? 0) / 2, py = (el?.clientHeight ?? 0) / 2;
    const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, t.scale * k));
    const r = scale / t.scale;
    return clampTf({ scale, tx: px - r * (px - t.tx), ty: py - r * (py - t.ty) });
  }), [clampTf]);

  const resetView = useCallback(() => setTf(clampTf({ scale: 1, tx: 0, ty: 0 })), [clampTf]);

  // Native, NOT React's onWheel: React binds wheel passively at the root, so
  // preventDefault() there is ignored ("Unable to preventDefault inside passive
  // event listener") and the page scrolls behind the zoom.
  const onWheel = useCallback((e: WheelEvent) => {
    e.preventDefault();
    const el = surfaceRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    setTf(t => {
      const scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, t.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      const k = scale / t.scale;
      return clampTf({ scale, tx: px - k * (px - t.tx), ty: py - k * (py - t.ty) });
    });
  }, [clampTf]);

  // Callback ref, not an effect: the surface mounts only once the sitemaps have
  // loaded (the empty/loading branch returns earlier), so a mount-time effect
  // with stable deps would bind to nothing and never retry.
  const attachSurface = useCallback((node: HTMLDivElement | null) => {
    surfaceRef.current?.removeEventListener('wheel', onWheel);
    surfaceRef.current = node;
    node?.addEventListener('wheel', onWheel, { passive: false });
  }, [onWheel]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    drag.current = { x: e.clientX - tf.tx, y: e.clientY - tf.ty };
    panOrigin.current = { x: e.clientX, y: e.clientY };
    panMoved.current = false;
    setPanning(true);
  }, [tf.tx, tf.ty]);

  const onMouseMove = useCallback((e: React.MouseEvent) => {
    const d = drag.current;
    if (!d) return;
    const o = panOrigin.current;
    if (o && Math.hypot(e.clientX - o.x, e.clientY - o.y) >= DRAG_SLOP_PX) panMoved.current = true;
    // Resolve the offset HERE, not inside the updater: React can run the
    // updater during a later render, by which time endDrag has nulled
    // `drag.current` — reading it in there crashed the whole Map page whenever
    // a mousemove and the mouseup landed in one batch.
    const tx = e.clientX - d.x, ty = e.clientY - d.y;
    setTf(t => clampTf({ ...t, tx, ty }));
  }, [clampTf]);

  const endDrag = useCallback(() => { drag.current = null; setPanning(false); }, []);

  /** Pointer client coords → normalized (0–1) image coords, inverting the
   * pan/zoom transform. Returns null if the surface/image isn't measurable yet.
   *
   * `clamp` decides what "outside the plan" means. Dragging an existing marker
   * clamps — it slides along the edge rather than escaping the plan.
   * Click-to-place and calibration pass clamp=false, so a click that lands off
   * the image is rejected outright instead of silently pinning to the border. */
  const pointToNorm = useCallback((
    clientX: number, clientY: number, clamp = true,
  ): { x: number; y: number } | null => {
    const surface = surfaceRef.current, img = imgRef.current;
    if (!surface || !img) return null;
    const rect = surface.getBoundingClientRect();
    const w = img.clientWidth, h = img.clientHeight;
    if (!w || !h) return null;
    const x = (clientX - rect.left - tf.tx) / (tf.scale * w);
    const y = (clientY - rect.top - tf.ty) / (tf.scale * h);
    if (!clamp) return (x < 0 || x > 1 || y < 0 || y > 1) ? null : { x, y };
    return { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) };
  }, [tf.tx, tf.ty, tf.scale]);

  /** Called on map switch: a new plan has its own aspect, and keeping the
   *  previous map's would size the surface wrong (misplacing every dot) until
   *  the image loads. */
  const resetForNewMap = useCallback(() => {
    setTf({ scale: 1, tx: 0, ty: 0 });
    setImgAspect(null);
  }, []);

  return {
    surfaceRef, imgRef, tf, panning, imgAspect, panMoved,
    onImgLoad, clampTf, zoomBy, resetView, attachSurface,
    onMouseDown, onMouseMove, endDrag, pointToNorm, resetForNewMap,
  };
}
