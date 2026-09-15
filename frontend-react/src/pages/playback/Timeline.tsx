/**
 * Timeline.tsx — the single-player timeline canvas, ported from the legacy
 * pbDrawBase/pbDrawHead/pbDrawLabels + pointer handling. Two stacked canvases:
 * base (coverage green, gaps red, hour grid) redrawn only when the view/data
 * changes; head (playhead) redrawn on every position tick. Click = seek,
 * drag = pan, wheel = zoom (10 min – 7 days).
 */
import { useEffect, useRef } from 'react';

/**
 * Colours come from the live theme (CSS custom properties, inherited from
 * html[data-theme]), read at draw time. Footage is a COOL slate on a WARM empty
 * track so it reads against the tan UI without clashing with the orange accent;
 * the playhead is that accent, the one bright mark on the bar.
 */
function palette(el: Element) {
  const cs = getComputedStyle(el);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    bg: v('--tl-track', '#182231'),
    covered: v('--tl-footage', '#6E8299'),
    gap: v('--red', '#ef4444'),
    grid: v('--tl-grid', 'rgba(255,255,255,.06)'),
    head: v('--accent', '#F18A3B'),
  };
}

export interface Lane {
  label: string;
  range: { earliest: number; latest: number } | null;   // epoch seconds
  gaps: { start: number; end: number }[];               // epoch seconds
}

export interface TimelineProps {
  viewStart: number;                                    // ms
  viewEnd: number;                                      // ms
  range: { earliest: number; latest: number } | null;   // epoch seconds (single-lane)
  gaps: { start: number; end: number }[];               // epoch seconds (single-lane)
  /** Multi-camera: one coverage lane each, sharing this pan/zoom/playhead.
   *  When present it supersedes `range`/`gaps`. Same component, so the two
   *  players' timelines can never drift apart. */
  lanes?: Lane[];
  playhead: number | null;                               // epoch seconds
  /** slopSec widens tiny painted gaps so clicks on them register as "in gap". */
  onSeek: (epochSec: number, slopSec: number) => void;
  onViewChange: (startMs: number, endMs: number) => void;
}

const LANE_H_MULTI = 40;   // per-camera band height when showing lanes
const LANE_H_SINGLE = 60;  // the original single-lane height

export function Timeline(props: TimelineProps) {
  const contRef = useRef<HTMLDivElement>(null);
  const baseRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLCanvasElement>(null);
  const size = useRef({ w: 0, h: 0 });
  const eatClick = useRef(false);
  const p = useRef(props);
  p.current = props;

  const timeToPx = (ms: number, w: number) => {
    const { viewStart, viewEnd } = p.current;
    return ((ms - viewStart) / (viewEnd - viewStart)) * w;
  };

  const sizeCanvases = (): { w: number; h: number } | null => {
    const cont = contRef.current;
    if (!cont) return null;
    const rect = cont.getBoundingClientRect();
    const w = rect.width, hh = rect.height;
    if (!w || !hh) return null;
    if (w !== size.current.w || hh !== size.current.h) {
      const dpr = window.devicePixelRatio || 1;
      for (const c of [baseRef.current, headRef.current]) {
        if (c) { c.width = w * dpr; c.height = hh * dpr; }
      }
      size.current = { w, h: hh };
    }
    return { w, h: hh };
  };

  /** Single lane unless the caller passed `lanes` — same code path for both. */
  const lanesOf = (pr: TimelineProps): Lane[] =>
    pr.lanes ?? [{ label: '', range: pr.range, gaps: pr.gaps }];

  const drawBase = () => {
    const dims = sizeCanvases();
    const ctx = baseRef.current?.getContext('2d');
    const cont = contRef.current;
    if (!dims || !ctx || !cont) return;
    const { w, h } = dims;
    const C = palette(cont);
    const { viewStart, viewEnd } = p.current;
    const lanes = lanesOf(p.current);
    const multi = !!p.current.lanes;
    const laneH = h / lanes.length;
    const inset = multi ? 7 : 0;   // multi lanes read as separate bars
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, w, h);

    lanes.forEach((lane, i) => {
      const top = i * laneH + inset;
      const bh = laneH - inset * 2;
      const { range, gaps } = lane;
      if (range) {
        const s = Math.max(range.earliest * 1000, viewStart);
        const e = Math.min(range.latest * 1000, viewEnd);
        if (e > s) {
          ctx.fillStyle = C.covered;
          ctx.fillRect(timeToPx(s, w), top, timeToPx(e, w) - timeToPx(s, w), bh);
        }
      }
      if (gaps.length) {
        // In the multi-camera overview, gaps read as empty track (footage =
        // filled) rather than alarm-red — a sparsely-recorded camera shouldn't
        // paint a whole scary red bar. The focused single view keeps red gaps.
        ctx.fillStyle = multi ? C.bg : C.gap;
        // Beyond the indexed frontier (range.latest) coverage is meaningless —
        // the trailing "gap" is just footage not indexed yet. Clamp to it.
        const capMs = range ? range.latest * 1000 : Infinity;
        for (const g of gaps) {
          const gs = g.start * 1000, ge = Math.min(g.end * 1000, capMs);
          if (ge <= gs || ge <= viewStart || gs >= viewEnd) continue;
          const x1 = timeToPx(Math.max(gs, viewStart), w);
          const x2 = timeToPx(Math.min(ge, viewEnd), w);
          ctx.fillRect(x1, top, Math.max(2, x2 - x1), bh);
        }
      }
      if (multi && i > 0) {
        ctx.strokeStyle = C.grid;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, i * laneH); ctx.lineTo(w, i * laneH); ctx.stroke();
      }
    });

    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    const hourMs = 3600_000;
    for (let t = Math.ceil(viewStart / hourMs) * hourMs; t <= viewEnd; t += hourMs) {
      const x = timeToPx(t, w);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    }
  };

  const drawHead = () => {
    const dims = sizeCanvases();
    const ctx = headRef.current?.getContext('2d');
    const cont = contRef.current;
    if (!dims || !ctx || !cont) return;
    const { w, h } = dims;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const { playhead } = p.current;
    if (playhead == null) return;
    const px = timeToPx(playhead * 1000, w);
    if (px < 0 || px > w) return;
    const head = palette(cont).head;
    ctx.strokeStyle = head;
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    ctx.fillStyle = head;
    ctx.beginPath();
    ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 7);
    ctx.closePath(); ctx.fill();
  };

  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => { drawBase(); drawHead(); },
    [props.viewStart, props.viewEnd, props.range, props.gaps, props.lanes]);
  useEffect(() => { drawHead(); }, [props.playhead]);

  // Repaint when the dashboard theme toggles — the canvas colours are read from
  // CSS vars, so they don't update on their own.
  useEffect(() => {
    const obs = new MutationObserver(() => { drawBase(); drawHead(); });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);
  /* eslint-enable react-hooks/exhaustive-deps */

  // Resize → resize canvases + full redraw; wheel needs a non-passive native
  // listener (React's synthetic onWheel can't preventDefault reliably).
  useEffect(() => {
    const cont = contRef.current;
    if (!cont) return;
    const redraw = () => { drawBase(); drawHead(); };
    const ro = new ResizeObserver(redraw);
    ro.observe(cont);
    window.addEventListener('resize', redraw);
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = cont.getBoundingClientRect();
      const { viewStart, viewEnd, onViewChange } = p.current;
      const frac = (e.clientX - rect.left) / rect.width;
      const at = viewStart + frac * (viewEnd - viewStart);
      const span = Math.max(10 * 60_000, Math.min(7 * 24 * 3600_000,
        (viewEnd - viewStart) * (e.deltaY > 0 ? 1.3 : 0.77)));
      onViewChange(at - frac * span, at + (1 - frac) * span);
    };
    cont.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', redraw);
      cont.removeEventListener('wheel', onWheel);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onClick = (e: React.MouseEvent) => {
    if (eatClick.current) { eatClick.current = false; return; }
    const cont = contRef.current;
    if (!cont) return;
    const rect = cont.getBoundingClientRect();
    const { viewStart, viewEnd, onSeek } = p.current;
    const epoch = (viewStart + ((e.clientX - rect.left) / rect.width) * (viewEnd - viewStart)) / 1000;
    const slop = ((viewEnd - viewStart) / 1000 / rect.width) * 1.5; // widen tiny painted gaps
    onSeek(epoch, slop);
  };

  const onMouseDown = (e: React.MouseEvent) => {
    if (e.button === 2) return;
    const cont = contRef.current;
    if (!cont) return;
    const startX = e.clientX;
    const { viewStart: vs, viewEnd: ve } = p.current;
    let moved = false;
    const onMove = (ev: MouseEvent) => {
      const dx = ev.clientX - startX;
      if (Math.abs(dx) > 5) moved = true;
      if (!moved) return;
      const rect = cont.getBoundingClientRect();
      const dt = (dx / rect.width) * (ve - vs);
      p.current.onViewChange(vs - dt, ve - dt);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      if (moved) eatClick.current = true;
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  };

  // Hour labels under the canvas (interval follows the zoom level).
  const viewHours = (props.viewEnd - props.viewStart) / 3600_000;
  let interval = 3600_000;
  if (viewHours <= 1) interval = 10 * 60_000;
  else if (viewHours > 6) interval = 2 * 3600_000;
  const labels: { pct: number; text: string }[] = [];
  for (let t = Math.ceil(props.viewStart / interval) * interval; t <= props.viewEnd; t += interval) {
    const pct = ((t - props.viewStart) / (props.viewEnd - props.viewStart)) * 100;
    if (pct < 0 || pct > 100) continue;
    const d = new Date(t), pad = (n: number) => String(n).padStart(2, '0');
    labels.push({ pct, text: `${pad(d.getHours())}:${pad(d.getMinutes())}` });
  }

  const lanes = props.lanes;
  const laneH = lanes ? LANE_H_MULTI : LANE_H_SINGLE;
  const height = (lanes ? lanes.length : 1) * laneH;

  return (
    <>
      <div ref={contRef} className="pb-timeline" title="Click to seek · drag to pan · scroll to zoom"
           style={{ height }} onClick={onClick} onMouseDown={onMouseDown}>
        <canvas ref={baseRef} />
        <canvas ref={headRef} />
        {/* Camera names float over their lane (multi only), never intercepting
            the click-to-seek — the canvas stays full-width so x→time is exact. */}
        {lanes && lanes.map((lane, i) => (
          <span key={i} className="pb-lane-label"
                style={{ top: i * laneH + laneH / 2, transform: 'translateY(-50%)' }}>
            {lane.label}
          </span>
        ))}
      </div>
      <div className="pb-labels">
        {labels.map((l, i) => (
          <span key={i} style={{ position: 'absolute', left: `${l.pct}%`, transform: 'translateX(-50%)' }}>
            {l.text}
          </span>
        ))}
      </div>
    </>
  );
}
