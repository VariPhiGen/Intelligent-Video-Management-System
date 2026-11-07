/**
 * AnnotateLayer.tsx — draw-a-box annotation over the playback video, ported
 * from the legacy smart-nvr UI (its annotationCanvas block) with one addition
 * that layout forces here: LETTERBOX MATH.
 *
 * The player's <video> uses object-fit: contain inside a fixed-height wrap, so
 * the frame usually paints with bars on two sides. A box drawn in element
 * coordinates is meaningless to the NVR, whose /annotations contract is
 * SOURCE-VIDEO pixels — that is what a future indexer crops with, and the
 * whole value of an annotation is that the crop lands on the person, not on a
 * letterbox bar. So every pointer event is mapped through the displayed-rect
 * transform (scale + offsets), and the box is clamped to the frame.
 *
 * Two decisions inherited from the legacy implementation, kept on purpose:
 *
 *   * The frame is captured AT POINTER-DOWN, not at save. Between drawing and
 *     pressing Save the video may buffer, seek, or roll to the next chunk —
 *     the JPEG must be the exact frame the box was drawn on, so both are
 *     snapshotted in the same instant (the wall-clock epoch too).
 *   * The upload is the FULL frame plus the bbox, never a pre-cropped patch —
 *     a future indexer can re-crop with context; a crop cannot be un-cropped.
 *
 * Frame capture is possible at all because the chunk player feeds the <video>
 * same-origin blob: URLs (authenticated fetch → object URL), so the canvas is
 * never tainted. Pointing this at a cross-origin stream would break it — the
 * save fails loudly with a toast, not silently.
 *
 * The save also duplicates camera/timestamp as QUERY params: the API's /api/nvr
 * proxy audits annotation.created from them without parsing our multipart body
 * (routers/nvr.py). The NVR itself reads only the Form fields.
 *
 * ONE TOOLBAR, NOT THREE. This used to render up to two `.pb-annotate-bar`
 * elements at once — the class picker and the save actions — and that class is
 * `position: absolute; bottom; left: 50%; translateX(-50%)`. Identical
 * coordinates, so drawing a box stacked the buttons directly on top of the
 * picker and the dropdown sat over the video the whole time regardless. There
 * is now a single bar whose right-hand slot swaps between the instruction and
 * the actions, so the mode has exactly one piece of chrome at the bottom and
 * one badge at the top.
 *
 * The box is drawn in the SELECTED CLASS'S COLOUR, with its name on a chip.
 * An annotation's class is the whole point of drawing the box, and a fixed red
 * rectangle made the picker's value invisible at the moment it mattered.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { apiUpload } from '@/lib/api';
import { searchPeopleByImage, type PersonHit, type SearchResponse } from '@/lib/smartsearch';
import type { LabelClass } from '@/lib/labelClasses';
import { useToast } from '@/components/Toast';

/** Ignore accidental clicks: a real box is at least this many DISPLAY pixels. */
const MIN_BBOX_DISPLAY_PX = 8;
const JPEG_QUALITY = 0.92;

interface Box { x: number; y: number; w: number; h: number }   // element px

/** Black or white, whichever stays legible on `hex`. A class colour is chosen
 *  by an administrator from the whole wheel, so the chip's text cannot assume
 *  a dark background: Rec. 709 luminance picks the readable one. */
export function readableOn(hex: string): string {
  const m = /^#?([0-9a-fA-F]{6})$/.exec(hex);
  if (!m) return '#fff';
  const n = parseInt(m[1], 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.62 ? '#0B0E12' : '#fff';
}

/** The zoom factor currently applied to the canvas by an ancestor transform.
 *
 *  THE WHOLE OF THIS LAYER WORKS IN UNSCALED LAYOUT PIXELS. `clientWidth` is
 *  the untransformed box and `getBoundingClientRect().width` is the painted
 *  one, so their ratio is whatever digital zoom is in force — measured from
 *  the DOM rather than passed in, which keeps this file independent of
 *  useVideoZoom and correct when there is no zoom at all (ratio 1).
 *
 *  Dividing pointer coordinates by this is what makes a box drawn at 4x land
 *  on the same SOURCE pixels as one drawn at 1x. Without it the box is stored
 *  scaled, and the error is invisible here — it surfaces as a misplaced crop
 *  in a dataset, or a box in the wrong place in the QA reviewer's queue.
 */
function appliedScale(c: HTMLCanvasElement): number {
  const layout = c.clientWidth;
  if (!layout) return 1;
  const painted = c.getBoundingClientRect().width;
  return painted > 0 ? painted / layout : 1;
}

/** Where the video actually paints inside its element (object-fit: contain). */
function displayedRect(v: HTMLVideoElement, elW: number, elH: number) {
  const s = Math.min(elW / v.videoWidth, elH / v.videoHeight);
  const w = v.videoWidth * s, h = v.videoHeight * s;
  return { s, offX: (elW - w) / 2, offY: (elH - h) / 2, w, h };
}

export function AnnotateLayer({ videoRef, camera, classes, epochOf, onSaved, onSimilar,
                               stage }: {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  camera: string;
  /** The zoom/pan stage holding the <video>. The canvas is portalled into it
   *  so both carry the SAME transform — see `appliedScale`. Null or absent
   *  renders the canvas in place, which is the un-zoomed behaviour. */
  stage?: HTMLElement | null;
  /** Active fleet label classes. Empty = the taxonomy has not been set up, and
   *  the bar says so rather than offering a save the server would refuse. */
  classes: LabelClass[];
  /** Wall-clock epoch (s) of the CURRENT video position — the chunk player owns
   *  that arithmetic; this layer must not re-derive it. */
  epochOf: () => number | null;
  onSaved?: () => void;
  /** "Find similar" results (search-by-example over the drawn box). Absent =
   *  the button is not offered — Smart Search may not be deployed here. */
  onSimilar?: (outcome: SearchResponse<PersonHit>) => void;
}) {
  const toast = useToast();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [busy, setBusy] = useState(false);

  // Drag + capture state lives in refs (pointer events fire faster than React
  // renders); only what the UI must react to (the committed box) is state.
  const drag = useRef<{ startX: number; startY: number; x: number; y: number } | null>(null);
  const [committed, setCommitted] = useState<Box | null>(null);
  // Preselected, never blank. The frame is captured at pointer-down, so a
  // save rejected for a missing class would lose the exact pixels the box
  // was drawn on — the picker must never be in an unsaveable state.
  const [classKey, setClassKey] = useState(classes[0]?.key ?? '');
  const capture = useRef<{ blob: Blob; epoch: number; w: number; h: number } | null>(null);

  // The taxonomy is fetched by the parent AFTER mount, so the initial state
  // above can legitimately be '' — and a save carrying no class is exactly the
  // 422 this picker exists to prevent. Re-seed whenever the set of classes
  // changes and the current selection is not in it (first load, or a class
  // retired from under the operator).
  useEffect(() => {
    if (classes.length && !classes.some(c => c.key === classKey)) {
      setClassKey(classes[0].key);
    }
  }, [classes, classKey]);

  const active = classes.find(c => c.key === classKey) ?? classes[0];
  const activeColor = active?.color ?? '#ff4444';

  const redraw = useCallback((live?: Box | null) => {
    const c = canvasRef.current;
    const ctx = c?.getContext('2d');
    if (!c || !ctx) return;
    // LAYOUT size, deliberately not the painted size. Under digital zoom the
    // painted box is `scale` times larger, and backing the canvas with that
    // would allocate scale^2 as much memory — at 8x on a 1000px canvas with
    // dpr 2 that is roughly half a gigabyte for an overlay holding one
    // rectangle. The ancestor transform scales the rendered result for free.
    const w = c.clientWidth, h = c.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.max(1, Math.round(w * dpr));
      c.height = Math.max(1, Math.round(h * dpr));
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const box = live ?? committed;
    if (!box) return;
    ctx.save();
    ctx.globalAlpha = 0.14;
    ctx.fillStyle = activeColor;
    ctx.fillRect(box.x, box.y, box.w, box.h);
    ctx.restore();
    ctx.lineWidth = 2;
    ctx.strokeStyle = activeColor;
    ctx.strokeRect(box.x, box.y, box.w, box.h);

    // Name the box while it is being drawn. Above it by preference; inside when
    // the box starts at the top of the frame and there is no room above.
    const name = active?.label;
    if (!name) return;
    ctx.font = '600 11.5px ui-sans-serif, system-ui, -apple-system, sans-serif';
    const padX = 6, chipH = 18;
    const chipW = ctx.measureText(name).width + padX * 2;
    const chipY = box.y >= chipH + 3 ? box.y - chipH - 3 : box.y + 3;
    ctx.fillStyle = activeColor;
    ctx.fillRect(box.x, chipY, chipW, chipH);
    ctx.fillStyle = readableOn(activeColor);
    ctx.textBaseline = 'middle';
    ctx.fillText(name, box.x + padX, chipY + chipH / 2 + 0.5);
  }, [committed, activeColor, active?.label]);

  // Keep the backing store matched to the element across resizes — otherwise
  // the drawn box drifts off the pixels the pointer actually covered.
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ro = new ResizeObserver(() => redraw());
    ro.observe(c);
    redraw();
    return () => ro.disconnect();
  }, [redraw]);

  const point = (ev: React.PointerEvent): { x: number; y: number } => {
    const c = canvasRef.current!;
    const rect = c.getBoundingClientRect();
    // Divided by the applied zoom so `Box` is always unscaled layout pixels —
    // the one coordinate space this layer stores, draws and converts in.
    const k = appliedScale(c);
    return { x: (ev.clientX - rect.left) / k, y: (ev.clientY - rect.top) / k };
  };

  const onPointerDown = async (ev: React.PointerEvent) => {
    if (busy) return;
    const v = videoRef.current;
    const epoch = epochOf();
    if (!v || !v.videoWidth || epoch == null) {
      toast('No video frame to annotate yet', 'err');
      return;
    }
    // Snapshot frame + instant NOW — see the header for why not at save time.
    const off = document.createElement('canvas');
    off.width = v.videoWidth;
    off.height = v.videoHeight;
    off.getContext('2d')!.drawImage(v, 0, 0);
    const blob = await new Promise<Blob | null>(res => off.toBlob(res, 'image/jpeg', JPEG_QUALITY));
    if (!blob) {
      toast('Could not capture the frame (tainted canvas?)', 'err');
      return;
    }
    capture.current = { blob, epoch, w: v.videoWidth, h: v.videoHeight };
    (ev.target as HTMLElement).setPointerCapture(ev.pointerId);
    const p = point(ev);
    drag.current = { startX: p.x, startY: p.y, ...p };
    setCommitted(null);
  };

  const onPointerMove = (ev: React.PointerEvent) => {
    if (!drag.current) return;
    const p = point(ev);
    drag.current.x = p.x;
    drag.current.y = p.y;
    const d = drag.current;
    redraw({
      x: Math.min(d.startX, d.x), y: Math.min(d.startY, d.y),
      w: Math.abs(d.x - d.startX), h: Math.abs(d.y - d.startY),
    });
  };

  const onPointerUp = (ev: React.PointerEvent) => {
    if (!drag.current) return;
    try { (ev.target as HTMLElement).releasePointerCapture(ev.pointerId); } catch { /* gone */ }
    const d = drag.current;
    drag.current = null;
    const box: Box = {
      x: Math.min(d.startX, d.x), y: Math.min(d.startY, d.y),
      w: Math.abs(d.x - d.startX), h: Math.abs(d.y - d.startY),
    };
    if (box.w < MIN_BBOX_DISPLAY_PX || box.h < MIN_BBOX_DISPLAY_PX) {
      setCommitted(null);
      redraw(null);
      return; // an accidental click, not a box
    }
    setCommitted(box);
  };

  const discard = () => {
    setCommitted(null);
    capture.current = null;
    redraw(null);
  };

  // Latest-render handles for the key bindings above, which are bound once per
  // committed box and must not close over a stale save().
  const discardRef = useRef(discard);
  const saveRef = useRef<() => Promise<void>>(async () => {});

  // A box is drawn with the pointer, so the hands are already on the mouse —
  // but Escape to abandon and Enter to commit are what every drawing tool has
  // taught people to expect, and the alternative is aiming at a small pill.
  // Only bound while a box is committed, so annotate mode never swallows keys
  // the player wants (space, arrows) except when there is something to act on.
  useEffect(() => {
    if (!committed || busy) return;
    const onKey = (ev: KeyboardEvent) => {
      const t = ev.target as HTMLElement | null;
      if (t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;
      if (ev.key === 'Escape') { ev.preventDefault(); discardRef.current(); }
      if (ev.key === 'Enter') { ev.preventDefault(); void saveRef.current(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [committed, busy]);

  /** Element px → SOURCE px through the contain-transform, clamped to the
   *  frame so a drag that ends on a letterbox bar still yields a legal bbox.
   *  Shared by Save and Find-similar — the box they act on must be the same
   *  pixels, or the annotation on disk and the crop that was searched drift. */
  function sourceBox(): { cap: NonNullable<typeof capture.current>;
                          x1: number; y1: number; x2: number; y2: number } | null {
    const cap = capture.current;
    const box = committed;
    const c = canvasRef.current;
    const v = videoRef.current;
    if (!cap || !box || !c || !v) return null;
    // Layout size again, matching `point()`. Using the painted rect here would
    // divide zoomed coordinates by a zoomed scale and happen to cancel — until
    // someone changed one of the two, at which point annotations would quietly
    // start landing in the wrong place.
    const { s, offX, offY } = displayedRect(v, c.clientWidth, c.clientHeight);
    const clampX = (x: number) => Math.min(Math.max((x - offX) / s, 0), cap.w);
    const clampY = (y: number) => Math.min(Math.max((y - offY) / s, 0), cap.h);
    const x1 = clampX(box.x), y1 = clampY(box.y);
    const x2 = clampX(box.x + box.w), y2 = clampY(box.y + box.h);
    if (x2 - x1 < 1 || y2 - y1 < 1) {
      toast('That box lands outside the video frame', 'err');
      return null;
    }
    return { cap, x1, y1, x2, y2 };
  }

  async function save() {
    const src = sourceBox();
    if (!src) return;
    const { cap, x1, y1, x2, y2 } = src;

    const ts = new Date(cap.epoch * 1000).toISOString();
    const form = new FormData();
    form.append('camera', camera);
    form.append('timestamp', ts);
    form.append('bbox_x', String(x1));
    form.append('bbox_y', String(y1));
    form.append('bbox_w', String(x2 - x1));
    form.append('bbox_h', String(y2 - y1));
    form.append('frame_width', String(cap.w));
    form.append('frame_height', String(cap.h));
    form.append('class_key', classKey);
    form.append('image', cap.blob, 'annotation.jpg');

    setBusy(true);
    try {
      // camera/timestamp ride the query string too — audit-only, see header.
      await apiUpload(
        `/annotations?camera=${encodeURIComponent(camera)}&timestamp=${encodeURIComponent(ts)}`,
        form,
      );
      toast('Annotation saved');
      discard();
      onSaved?.();
    } catch (e: any) {
      toast(e.message || 'Could not save the annotation', 'err');
    } finally {
      setBusy(false);
    }
  }

  /** Search-by-example over the drawn box. The CROP goes to the index, never
   *  the full frame — the query should be the person, not the scene. Cropped
   *  from the captured frame (not the live element), so it is exactly the
   *  pixels the box was drawn on even if the video moved since. */
  async function findSimilar() {
    if (!onSimilar) return;
    const src = sourceBox();
    if (!src) return;
    const { cap, x1, y1, x2, y2 } = src;
    setBusy(true);
    try {
      const bmp = await createImageBitmap(cap.blob);
      const off = document.createElement('canvas');
      off.width = Math.max(1, Math.round(x2 - x1));
      off.height = Math.max(1, Math.round(y2 - y1));
      off.getContext('2d')!.drawImage(
        bmp, x1, y1, x2 - x1, y2 - y1, 0, 0, off.width, off.height,
      );
      bmp.close();
      const crop = await new Promise<Blob | null>(res => off.toBlob(res, 'image/jpeg', JPEG_QUALITY));
      if (!crop) { toast('Could not crop the frame', 'err'); return; }
      onSimilar(await searchPeopleByImage(crop));
    } catch (e: any) {
      toast(e.message || 'Similarity search failed', 'err');
    } finally {
      setBusy(false);
    }
  }

  discardRef.current = discard;
  saveRef.current = save;

  const canSave = !!committed && classes.length > 0 && !!classKey;

  const canvas = (
    <canvas
      ref={canvasRef}
      className="pb-annotate-canvas"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
    />
  );

  return (
    <>
      {/* Into the zoom stage when there is one, so the canvas and the video
          carry the same transform. The toolbar and badge below stay OUTSIDE
          it: chrome that scaled with the footage would grow to illegibility
          and pan off the edge of the player. */}
      {stage ? createPortal(canvas, stage) : canvas}
      {/* Mode badge only. It says why the cursor is a crosshair and why clicks
          are being swallowed; the instruction belongs on the toolbar, next to
          the controls it describes, rather than being a second thing to read in
          the opposite corner. */}
      <div className="pb-annotate-hint">
        <span className="badge-dot" />Annotating
      </div>

      {/* The class is chosen BEFORE the box, not after. An operator should know
          what they are labelling while they draw it — and an empty taxonomy has
          to be visible before the drag, because the frame is captured at
          pointer-down and a save that bounces cannot be retried from the same
          pixels. */}
      {classes.length === 0 ? (
        <div className="pb-annotate-bar is-empty">
          <span className="pb-annotate-empty">
            <b>No label classes yet.</b> An administrator adds them under
            Administration → Label classes, then a box can be saved.
          </span>
        </div>
      ) : (
        <div className="pb-annotate-bar">
          <label className="pb-annotate-class">
            <span className="pb-annotate-swatch" style={{ background: activeColor }} />
            <span className="pb-annotate-class-label">Class</span>
            <select value={classKey} disabled={busy}
                    onChange={e => setClassKey(e.target.value)}>
              {classes.map(c => (
                <option key={c.key} value={c.key}>{c.label}</option>
              ))}
            </select>
          </label>

          <span className="pb-annotate-sep" />

          {/* One slot, two states — never two bars in the same place. */}
          {committed ? (
            <div className="pb-annotate-actions">
              <button className="btn-primary btn-sm" disabled={busy || !canSave}
                      onClick={save} title="Save this box and its frame (Enter)">
                {busy ? 'Working…' : 'Save'}
              </button>
              {onSimilar && (
                <button className="btn-secondary btn-sm" disabled={busy} onClick={findSimilar}
                        title="Search the person index for this crop (search-by-example)">
                  ✦ Find similar
                </button>
              )}
              <button className="btn-ghost btn-sm iconish" disabled={busy}
                      onClick={discard} title="Discard this box (Esc)"
                      aria-label="Discard this box">✕</button>
            </div>
          ) : (
            <span className="pb-annotate-tip">Drag a box around a person or object</span>
          )}
        </div>
      )}
    </>
  );
}
