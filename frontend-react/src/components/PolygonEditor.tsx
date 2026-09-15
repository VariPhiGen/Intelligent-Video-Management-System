/**
 * PolygonEditor — draw shapes over a live camera snapshot, in normalized 0–1
 * coordinates. Two draw modes: 'polygon' (privacy masks, analytics zones — 3+
 * points, click-to-close) and 'line' (analytics tripwires — exactly 2 points).
 * Renders any number of existing shapes (zones filled, tripwires as dashed
 * lines with their ENTRY arrow across them) plus the in-progress one.
 *
 * The parent owns WHAT is being drawn (drawing/drawMode/drawColor) and receives
 * the finished shape via onFinish; the editor owns the in-progress point buffer.
 */
import { useEffect, useRef, useState } from 'react';
import { apiBlob } from '@/lib/api';
import { entryVector } from '@/lib/tripwire';

const CANVAS_W = 720;
const CANVAS_H_DEFAULT = 405;

export interface EditorPolygon {
  points: number[][];                       // normalized 0–1
  color: string;
  label?: string;
  kind?: 'zone' | 'tripwire';               // default 'zone' (filled polygon)
  direction?: 'both' | 'a2b' | 'b2a';       // tripwire only — which way across is Entry (lib/tripwire.ts)
}

function arrowhead(ctx: CanvasRenderingContext2D, from: number[], to: number[], color: string, W: number, H: number) {
  const bx = to[0] * W, by = to[1] * H;
  const ang = Math.atan2(by - from[1] * H, bx - from[0] * W);
  const s = 9;
  ctx.beginPath();
  ctx.moveTo(bx, by);
  ctx.lineTo(bx - s * Math.cos(ang - Math.PI / 6), by - s * Math.sin(ang - Math.PI / 6));
  ctx.lineTo(bx - s * Math.cos(ang + Math.PI / 6), by - s * Math.sin(ang + Math.PI / 6));
  ctx.closePath();
  ctx.fillStyle = color; ctx.fill();
}

export function PolygonEditor({
  cameraId, polygons, drawing, drawColor = '#facc15', drawMode = 'polygon',
  onFinish, onCancel, onPendingChange, noun = 'zone', minPoints = 3, readOnly = false,
}: {
  cameraId: string;
  polygons: EditorPolygon[];
  drawing: boolean;
  /** Display-only surface (e.g. the coverage map): no drawing is ever offered
   *  here, so a missing frame must not promise that you can draw on it. */
  readOnly?: boolean;
  drawColor?: string;
  drawMode?: 'polygon' | 'line';
  onFinish: (points: number[][]) => void;
  onCancel?: () => void;
  onPendingChange?: (pointCount: number) => void;
  noun?: string;
  minPoints?: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  const [loading, setLoading] = useState(true);
  const [snapErr, setSnapErr] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [cur, setCur] = useState<number[][]>([]);
  const need = drawMode === 'line' ? 2 : minPoints;

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { onPendingChange?.(cur.length); }, [cur.length]);

  // Snapshot backdrop needs the Bearer token — a plain <img src> can't send it.
  //
  // Retried, because a first miss here is usually transient: the camera is
  // mid-reconnect, or another part of the UI is already decoding a frame for
  // the same camera. Giving up on attempt one is what left operators drawing
  // masks and zones over an empty rectangle.
  useEffect(() => {
    setImg(null); setLoading(true); setSnapErr(null);
    let alive = true;
    let objUrl: string | null = null;

    (async () => {
      for (let attempt = 0; attempt < 3 && alive; attempt++) {
        try {
          const blob = await apiBlob(`/cameras/${cameraId}/snapshot?t=${Date.now()}`);
          if (!alive) return;
          objUrl = URL.createObjectURL(blob);
          const image = new Image();
          image.onload = () => { if (alive) { setImg(image); setLoading(false); setSnapErr(null); } };
          image.onerror = () => { if (alive) { setLoading(false); setSnapErr('Frame could not be decoded'); } };
          image.src = objUrl;
          return;
        } catch (e: any) {
          if (!alive) return;
          if (attempt === 2) { setLoading(false); setSnapErr(e?.message || 'Snapshot unavailable'); return; }
          await new Promise(r => setTimeout(r, 1200 * (attempt + 1)));
        }
      }
    })();

    return () => { alive = false; if (objUrl) URL.revokeObjectURL(objUrl); };
  }, [cameraId, reloadKey]);

  useEffect(() => { if (!drawing) setCur([]); }, [drawing]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const H = img ? (Math.round(CANVAS_W * img.height / img.width) || CANVAS_H_DEFAULT) : CANVAS_H_DEFAULT;
    if (canvas.height !== H) canvas.height = H;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const W = canvas.width;
    ctx.clearRect(0, 0, W, H);
    if (img) {
      ctx.drawImage(img, 0, 0, W, H);
    } else {
      ctx.fillStyle = '#0a0e13'; ctx.fillRect(0, 0, W, H);
      ctx.fillStyle = '#5b7186'; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('No live snapshot — drawing on a blank frame (coordinates still apply)', W / 2, H / 2);
      ctx.textAlign = 'left';
    }
    polygons.forEach(p => {
      if (p.points.length < 2) return;
      if (p.kind === 'tripwire') {
        const [a, b] = p.points;
        ctx.beginPath();
        ctx.moveTo(a[0] * W, a[1] * H); ctx.lineTo(b[0] * W, b[1] * H);
        ctx.strokeStyle = p.color; ctx.lineWidth = 2.5; ctx.setLineDash([6, 4]); ctx.stroke(); ctx.setLineDash([]);
        [a, b].forEach(pt => { ctx.beginPath(); ctx.arc(pt[0] * W, pt[1] * H, 3.5, 0, Math.PI * 2); ctx.fillStyle = p.color; ctx.fill(); });
        // The line is the gateway; the arrow is the way people WALK to enter —
        // across the line from its middle, never along it.
        const mx = ((a[0] + b[0]) / 2) * W, my = ((a[1] + b[1]) / 2) * H;
        const v = entryVector(p.points, p.direction, W, H);
        if (v) {
          const len = 34;
          const tip = [mx + v[0] * len, my + v[1] * len];
          ctx.beginPath(); ctx.moveTo(mx - v[0] * 10, my - v[1] * 10); ctx.lineTo(tip[0], tip[1]);
          ctx.strokeStyle = p.color; ctx.lineWidth = 3; ctx.stroke();
          arrowhead(ctx, [mx / W, my / H], [tip[0] / W, tip[1] / H], p.color, W, H);
          ctx.fillStyle = p.color; ctx.font = 'bold 11px sans-serif'; ctx.textAlign = 'center';
          ctx.fillText('ENTRY', mx + v[0] * (len + 14), my + v[1] * (len + 14) + 4); ctx.textAlign = 'left';
        }
        if (p.label || !v) {
          // The name sits on the side away from the arrow so the two never overlap.
          const text = v ? (p.label ?? '') : `${p.label ? `${p.label} · ` : ''}entry direction not set`;
          ctx.fillStyle = p.color; ctx.font = '11px monospace'; ctx.textAlign = 'center';
          ctx.fillText(text, mx - (v ? v[0] * 16 : 0), my - (v ? v[1] * 16 : 8) + 4); ctx.textAlign = 'left';
        }
      } else {
        ctx.beginPath();
        p.points.forEach(([x, y], j) => j ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H));
        ctx.closePath();
        ctx.fillStyle = p.color + '33'; ctx.fill();
        ctx.strokeStyle = p.color; ctx.lineWidth = 2; ctx.stroke();
        if (p.label) {
          const cx = (p.points.reduce((s, pt) => s + pt[0], 0) / p.points.length) * W;
          const cy = (p.points.reduce((s, pt) => s + pt[1], 0) / p.points.length) * H;
          ctx.fillStyle = p.color; ctx.font = '11px monospace'; ctx.textAlign = 'center';
          ctx.fillText(p.label, cx, cy); ctx.textAlign = 'left';
        }
      }
    });
    if (cur.length) {
      ctx.beginPath();
      cur.forEach(([x, y], i) => i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H));
      ctx.strokeStyle = drawColor; ctx.lineWidth = 2; ctx.setLineDash([5, 4]); ctx.stroke(); ctx.setLineDash([]);
      cur.forEach(([x, y], i) => {
        ctx.beginPath();
        ctx.arc(x * W, y * H, i === 0 ? 6 : 4, 0, Math.PI * 2);
        ctx.fillStyle = i === 0 ? '#4ade80' : drawColor;
        ctx.fill();
      });
    }
  }, [img, polygons, cur, drawColor]);

  function finish() {
    if (cur.length < need) return;
    onFinish(cur.slice(0, drawMode === 'line' ? 2 : cur.length));
    setCur([]);
  }
  function onCanvasClick(e: React.MouseEvent<HTMLCanvasElement>) {
    if (!drawing) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const x = Math.min(Math.max((e.clientX - rect.left) / rect.width, 0), 1);
    const y = Math.min(Math.max((e.clientY - rect.top) / rect.height, 0), 1);
    if (drawMode === 'line') {
      const np = [...cur, [x, y]];
      if (np.length >= 2) { onFinish(np.slice(0, 2)); setCur([]); }
      else setCur(np);
      return;
    }
    if (cur.length >= need) {
      const [fx, fy] = cur[0];
      if (Math.hypot((fx - x) * rect.width, (fy - y) * rect.height) < 10) { finish(); return; }
    }
    setCur(c => [...c, [x, y]]);
  }

  return (
    <div>
      <div style={{ position: 'relative' }}>
        <canvas ref={canvasRef} width={CANVAS_W} height={CANVAS_H_DEFAULT}
                onClick={onCanvasClick}
                onDoubleClick={e => { e.preventDefault(); finish(); }}
                style={{ width: '100%', borderRadius: 12, border: '1px solid var(--border2)',
                         boxShadow: 'var(--shadow)', cursor: drawing ? 'crosshair' : 'default',
                         background: '#0a0e13', display: 'block' }} />
        {loading && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center',
                        gap: 10, borderRadius: 12, background: 'rgba(10,14,19,.55)', color: 'var(--muted)', fontSize: 12.5 }}>
            <span className="spinner" /> Loading camera frame…
          </div>
        )}
        {/* A refresh is cheap (the backend caches the frame for a few seconds)
            and the alternative — closing and reopening the tab — is not. */}
        {!loading && (
          <button className="btn-ghost btn-sm" onClick={() => setReloadKey(k => k + 1)}
                  title="Pull a fresh frame from the camera"
                  style={{ position: 'absolute', top: 8, right: 8 }}>
            ⟳ Refresh frame
          </button>
        )}
      </div>
      {!loading && !img && (
        <div style={{ marginTop: 8, fontSize: 12, color: 'var(--warn, #f5a524)' }}>
          {readOnly
            ? `${snapErr ?? 'No live frame from this camera'} — the shapes below are still in effect.`
            : `${snapErr ?? 'No live frame from this camera'} — you can still draw; coordinates are stored relative to the frame and apply once the stream is back.`}
        </div>
      )}
      {drawing && (
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>
            {drawMode === 'line'
              ? (cur.length ? 'Click the second point to finish the tripwire' : 'Click two points to draw a tripwire')
              : (cur.length ? `Drawing · ${cur.length} point${cur.length === 1 ? '' : 's'}` : 'Click the frame to drop points')}
          </span>
          <span style={{ flex: 1 }} />
          {drawMode !== 'line' &&
            <button className="btn-primary btn-sm" disabled={cur.length < need} onClick={finish}>✓ Finish {noun}</button>}
          <button className="btn-ghost btn-sm" disabled={!cur.length} onClick={() => setCur(c => c.slice(0, -1))}>↶ Undo point</button>
          <button className="btn-ghost btn-sm" onClick={() => { setCur([]); onCancel?.(); }}>Cancel</button>
        </div>
      )}
    </div>
  );
}
