/**
 * useVideoZoom.ts — digital zoom and pan over the playback video.
 *
 * SINGLE PLAYBACK ONLY, on purpose. The multi-view runs up to nine <video>
 * elements; nine independently transformed compositing layers is a different
 * performance question, and the annotate overlay — the main reason to zoom at
 * all — only exists on the single player.
 *
 * THE TRANSFORM GOES ON A STAGE THAT CONTAINS THE VIDEO *AND* THE ANNOTATE
 * CANVAS, never on the <video> alone. That is not a layout preference, it is
 * what keeps annotations correct. AnnotateLayer maps pointer events to source
 * pixels by measuring its own canvas with getBoundingClientRect(); as long as
 * the canvas is scaled by the same transform as the video, the scale factor
 * divides out of that mapping exactly and a box drawn at 4x lands on the same
 * source pixels as one drawn at 1x. Scale the video on its own and the canvas
 * still reports its untransformed box — the maths keeps working and silently
 * produces wrong coordinates, which travel into the NVR, the COCO export and
 * the QA review payload with nothing to flag them.
 *
 * Pan is clamped so the frame can never be dragged off the stage, matching
 * pages/map/usePanZoom. Zoom is anchored at the pointer, because zooming to
 * the centre when you are looking at a corner means chasing the thing you
 * wanted to inspect.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export interface ZoomTransform { scale: number; tx: number; ty: number }

export const MIN_ZOOM = 1;
export const MAX_ZOOM = 8;
const STEP = 1.4;

/** Below this the frame is "not zoomed": pan is pointless and the controls say
 *  so. Not exactly 1 because a wheel notch can leave 1.0000000002 behind. */
const EPSILON = 0.01;

export const IDENTITY: ZoomTransform = { scale: 1, tx: 0, ty: 0 };

/** Keep the frame covering the stage: never a gap at an edge, centred at 1x. */
export function clamp(t: ZoomTransform, w: number, h: number): ZoomTransform {
  const scale = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, t.scale));
  // At scale 1 the content exactly fills the stage, so the only legal offset
  // is zero; beyond that the slack is (scale - 1) * size in each axis.
  const slackX = (scale - 1) * w, slackY = (scale - 1) * h;
  // `+ 0` normalises negative zero. Math.min(0, -0) is -0, which is a correct
  // offset and a confusing value: it serialises into the transform as
  // "translate(-0px)" and compares unequal to 0 under Object.is. Nothing
  // downstream deserves to care.
  return {
    scale,
    tx: Math.min(0, Math.max(-slackX, t.tx)) + 0,
    ty: Math.min(0, Math.max(-slackY, t.ty)) + 0,
  };
}

/** Zoom about a point in stage coordinates, keeping that point still.
 *
 *  Exported because it is the one piece of arithmetic here worth testing
 *  directly: an anchor that drifts is the difference between "zoom into what
 *  I am pointing at" and "zoom somewhere near it and then hunt".
 */
export function zoomAbout(t: ZoomTransform, nextScale: number,
                          px: number, py: number,
                          w: number, h: number): ZoomTransform {
  const scale = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, nextScale));
  // The content point under the pointer, in unscaled stage coordinates.
  const ux = (px - t.tx) / t.scale, uy = (py - t.ty) / t.scale;
  // Put that same content point back under the pointer at the new scale.
  return clamp({ scale, tx: px - ux * scale, ty: py - uy * scale }, w, h);
}

export function useVideoZoom() {
  const stageRef = useRef<HTMLDivElement | null>(null);
  // Callback ref as STATE, not a plain ref: AnnotateLayer portals its canvas
  // into this node, and a portal target has to trigger a re-render when it
  // appears. A useRef would be populated after the render that needed it.
  const [stageEl, setStageEl] = useState<HTMLDivElement | null>(null);
  const attachStage = useCallback((node: HTMLDivElement | null) => {
    stageRef.current = node;
    setStageEl(node);
  }, []);

  const [tf, setTf] = useState<ZoomTransform>(IDENTITY);
  const zoomed = tf.scale > MIN_ZOOM + EPSILON;

  const size = () => {
    const el = stageRef.current;
    return { w: el?.clientWidth ?? 0, h: el?.clientHeight ?? 0 };
  };

  const reset = useCallback(() => setTf(IDENTITY), []);

  /** Step in or out about the stage centre — what the buttons and keys use. */
  const step = useCallback((direction: 1 | -1) => {
    const { w, h } = size();
    setTf(t => zoomAbout(t, t.scale * (direction > 0 ? STEP : 1 / STEP),
                         w / 2, h / 2, w, h));
  }, []);

  // Native listener, not React's onWheel: React binds wheel passively at the
  // root, so preventDefault() there is ignored and the page scrolls while you
  // are trying to zoom. Same reason Timeline.tsx and usePanZoom do this.
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const box = el.getBoundingClientRect();
      // Pointer position in UNSCALED stage coordinates: the box is the
      // transformed one, so undo the layout offset only.
      const px = e.clientX - box.left, py = e.clientY - box.top;
      const factor = e.deltaY < 0 ? STEP : 1 / STEP;
      setTf(t => zoomAbout(t, t.scale * factor, px, py,
                           el.clientWidth, el.clientHeight));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [stageEl]);

  // ── Panning ────────────────────────────────────────────────────────────
  // Middle-button or space-free drag is not offered: the left button belongs
  // to the annotate layer, which sits on top of the stage and must keep every
  // pointer event it gets. Panning rides the RIGHT button instead, so zooming
  // in to draw a box never fights the drawing itself.
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);
  const [panning, setPanning] = useState(false);

  const onPanStart = useCallback((e: React.PointerEvent) => {
    if (e.button !== 2) return;
    e.preventDefault();
    drag.current = { x: e.clientX, y: e.clientY, tx: tf.tx, ty: tf.ty };
    setPanning(true);
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }, [tf.tx, tf.ty]);

  const onPanMove = useCallback((e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const { w, h } = size();
    setTf(t => clamp({ scale: t.scale,
                       tx: d.tx + (e.clientX - d.x),
                       ty: d.ty + (e.clientY - d.y) }, w, h));
  }, []);

  const onPanEnd = useCallback(() => {
    drag.current = null;
    setPanning(false);
  }, []);

  // A resize changes the slack, so an offset legal a moment ago can leave a
  // gap at an edge. Re-clamp rather than reset: losing the operator's zoom
  // because the window moved is its own small betrayal.
  useEffect(() => {
    const el = stageRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => {
      setTf(t => clamp(t, el.clientWidth, el.clientHeight));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [stageEl]);

  return {
    tf, zoomed, panning,
    attachStage, stageEl,
    zoomIn: () => step(1),
    zoomOut: () => step(-1),
    reset,
    onPanStart, onPanMove, onPanEnd,
    /** What to put on the stage's `style`. */
    style: {
      transform: `translate(${tf.tx}px, ${tf.ty}px) scale(${tf.scale})`,
      transformOrigin: '0 0',
    } as React.CSSProperties,
  };
}
