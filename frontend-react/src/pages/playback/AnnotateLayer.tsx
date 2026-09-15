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
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiUpload } from '@/lib/api';
import { searchPeopleByImage, type PersonHit, type SearchResponse } from '@/lib/smartsearch';
import { useToast } from '@/components/Toast';

/** Ignore accidental clicks: a real box is at least this many DISPLAY pixels. */
const MIN_BBOX_DISPLAY_PX = 8;
const JPEG_QUALITY = 0.92;

interface Box { x: number; y: number; w: number; h: number }   // element px

/** Where the video actually paints inside its element (object-fit: contain). */
function displayedRect(v: HTMLVideoElement, elW: number, elH: number) {
  const s = Math.min(elW / v.videoWidth, elH / v.videoHeight);
  const w = v.videoWidth * s, h = v.videoHeight * s;
  return { s, offX: (elW - w) / 2, offY: (elH - h) / 2, w, h };
}

export function AnnotateLayer({ videoRef, camera, epochOf, onSaved, onSimilar }: {
  videoRef: React.RefObject<HTMLVideoElement | null>;
  camera: string;
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
  const capture = useRef<{ blob: Blob; epoch: number; w: number; h: number } | null>(null);

  const redraw = useCallback((live?: Box | null) => {
    const c = canvasRef.current;
    const ctx = c?.getContext('2d');
    if (!c || !ctx) return;
    const rect = c.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (c.width !== Math.round(rect.width * dpr) || c.height !== Math.round(rect.height * dpr)) {
      c.width = Math.max(1, Math.round(rect.width * dpr));
      c.height = Math.max(1, Math.round(rect.height * dpr));
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    const box = live ?? committed;
    if (!box) return;
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#ff4444';
    ctx.fillStyle = 'rgba(255,68,68,.12)';
    ctx.fillRect(box.x, box.y, box.w, box.h);
    ctx.strokeRect(box.x, box.y, box.w, box.h);
  }, [committed]);

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
    const rect = canvasRef.current!.getBoundingClientRect();
    return { x: ev.clientX - rect.left, y: ev.clientY - rect.top };
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
    const rect = c.getBoundingClientRect();
    const { s, offX, offY } = displayedRect(v, rect.width, rect.height);
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
    form.append('image', cap.blob, 'annotation.jpg');

    setBusy(true);
    try {
      // camera/timestamp ride the query string too — audit-only, see header.
      await apiUpload(
        `/nvr/annotations?camera=${encodeURIComponent(camera)}&timestamp=${encodeURIComponent(ts)}`,
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

  return (
    <>
      <canvas
        ref={canvasRef}
        className="pb-annotate-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      />
      <div className="pb-annotate-hint">
        Drag a box around a person or object — the frame and box are saved together
      </div>
      {committed && (
        <div className="pb-annotate-bar">
          <button className="btn-primary btn-sm" disabled={busy} onClick={save}>
            {busy ? 'Working…' : 'Save annotation'}
          </button>
          {onSimilar && (
            <button className="btn-secondary btn-sm" disabled={busy} onClick={findSimilar}
                    title="Search the person index for this crop (search-by-example)">
              ✦ Find similar
            </button>
          )}
          <button className="btn-ghost btn-sm" disabled={busy} onClick={discard}>Discard</button>
        </div>
      )}
    </>
  );
}
