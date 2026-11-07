/**
 * Timeline.tsx — the single-player timeline canvas, ported from the legacy
 * pbDrawBase/pbDrawHead/pbDrawLabels + pointer handling. Two stacked canvases:
 * base (coverage green, gaps red, hour grid) redrawn only when the view/data
 * changes; head (playhead) redrawn on every position tick. Click = seek,
 * drag = pan, wheel = zoom (10 min – 7 days).
 *
 * Single lane: an optional marker strip under the footage bar
 * (timelineMarks.ts). Thin person / vehicle / motion rows show when a tracked
 * object was in view or something moved; the events row below them shows AI
 * events — intervals as bars, point events as diamonds, in their activity's
 * colour. Hover shows what it was; click seeks to it.
 *
 * Multi lane: each lane may carry its camera's marks, drawn as a thin band in
 * the gap under its footage bar — an overview of where to look, not a second
 * strip per camera. Clicking there seeks every tile, like anywhere on a lane.
 */
import { useEffect, useRef, useState } from 'react';
import { hitMark, type PresenceKind, type TimelineMark } from './timelineMarks';

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
    person: v('--tl-person', '#6366F1'),
    vehicle: v('--tl-vehicle', '#0D9488'),
    motion: v('--tl-motion', '#B8457A'),
  };
}

export interface Lane {
  label: string;
  range: { earliest: number; latest: number } | null;   // epoch seconds
  gaps: { start: number; end: number }[];               // epoch seconds
  /** Multi only: this camera's markers, drawn as a band under its lane. */
  marks?: TimelineMark[];
  /**
   * Multi only, sync lock off: this camera's own position (epoch seconds),
   * drawn as a head confined to its lane. Present on any lane → every lane
   * draws its own head and the shared `playhead` is not drawn.
   */
  playhead?: number | null;
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
  /** slopSec widens tiny painted gaps so clicks on them register as "in gap".
   *  `lane` is the index of the lane clicked (multi only). */
  onSeek: (epochSec: number, slopSec: number, lane?: number) => void;
  onViewChange: (startMs: number, endMs: number) => void;
  /** Single lane only: events drawn in a strip under the footage bar. Passing
   *  the prop (even empty) reserves the strip, so the bar does not jump in
   *  height as marks load. */
  marks?: TimelineMark[];
  /** Clicking a mark; without it a click in the strip seeks like anywhere else. */
  onMarkClick?: (mark: TimelineMark) => void;
  /** Presence rows to reserve above the events row, top to bottom — the kinds
   *  this camera indexes. Reserved even while empty, like `marks`. */
  presenceRows?: PresenceKind[];
}

const LANE_H_MULTI = 40;   // per-camera band height when showing lanes
const LANE_H_SINGLE = 60;  // the original single-lane height
const MARK_STRIP_H = 18;   // the event row under a single lane
const PRESENCE_ROW_H = 8;  // each person / vehicle row above it

function fmtClock(epochSec: number): string {
  const d = new Date(epochSec * 1000), pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function Timeline(props: TimelineProps) {
  const contRef = useRef<HTMLDivElement>(null);
  const baseRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLCanvasElement>(null);
  const size = useRef({ w: 0, h: 0 });
  const eatClick = useRef(false);
  const [hover, setHover] = useState<{ mark: TimelineMark; x: number } | null>(null);
  const p = useRef(props);
  p.current = props;

  const rowsOf = (pr: TimelineProps) => pr.presenceRows ?? [];
  /** Height of the marker strip, 0 when this timeline has none. */
  const stripOf = (pr: TimelineProps) =>
    (pr.marks && !pr.lanes ? MARK_STRIP_H + rowsOf(pr).length * PRESENCE_ROW_H : 0);

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
    const strip = stripOf(p.current);
    const laneH = (h - strip) / lanes.length;
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
      if (multi && lane.marks?.length) drawLaneMarks(ctx, w, top + bh + 1, lane.marks, C);
      if (multi && i > 0) {
        ctx.strokeStyle = C.grid;
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(0, i * laneH); ctx.lineTo(w, i * laneH); ctx.stroke();
      }
    });

    if (strip) drawMarks(ctx, w, h - strip, strip, C);

    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    const hourMs = 3600_000;
    for (let t = Math.ceil(viewStart / hourMs) * hourMs; t <= viewEnd; t += hourMs) {
      const x = timeToPx(t, w);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    }
  };

  /** The strip: a hairline under the footage, a thin bar row per presence
   *  kind, then the events row — intervals as bars at least 3px wide, point
   *  events as diamonds. */
  const drawMarks = (ctx: CanvasRenderingContext2D, w: number, top: number, _sh: number,
                     C: ReturnType<typeof palette>) => {
    const { viewStart, viewEnd, marks = [] } = p.current;
    const rows = rowsOf(p.current);
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, top + 0.5); ctx.lineTo(w, top + 0.5); ctx.stroke();
    const visible = (m: TimelineMark) =>
      !((m.end ?? m.start) * 1000 < viewStart - 60_000 || m.start * 1000 > viewEnd + 60_000);

    rows.forEach((kind, i) => {
      const rowTop = top + i * PRESENCE_ROW_H;
      ctx.fillStyle = C[kind];
      ctx.globalAlpha = 0.85;
      for (const m of marks) {
        if (m.row !== kind || !visible(m)) continue;
        const x1 = timeToPx(m.start * 1000, w);
        const x2 = timeToPx((m.end ?? m.start) * 1000, w);
        ctx.fillRect(x1, rowTop + 2, Math.max(2, x2 - x1), PRESENCE_ROW_H - 3);
      }
      ctx.globalAlpha = 1;
    });

    const evTop = top + rows.length * PRESENCE_ROW_H;
    const mid = evTop + MARK_STRIP_H / 2;
    const r = Math.min(5, MARK_STRIP_H / 2 - 2);
    for (const m of marks) {
      if (m.row !== 'event' || !visible(m)) continue;
      const sMs = m.start * 1000;
      const eMs = (m.end ?? m.start) * 1000;
      ctx.fillStyle = m.color;
      const x1 = timeToPx(sMs, w);
      if (m.end != null) {
        const x2 = timeToPx(eMs, w);
        ctx.globalAlpha = 0.85;
        ctx.fillRect(x1, mid - 3, Math.max(3, x2 - x1), 6);
        ctx.globalAlpha = 1;
      }
      // Every mark starts with a diamond, so an interval's start reads as
      // precisely as a point event and a click target is never 1px wide.
      ctx.beginPath();
      ctx.moveTo(x1, mid - r); ctx.lineTo(x1 + r, mid); ctx.lineTo(x1, mid + r); ctx.lineTo(x1 - r, mid);
      ctx.closePath(); ctx.fill();
    }
  };

  /** Multi lane: every mark as a 4px tick band in the lane's lower gap —
   *  presence in its kind's colour, events in their activity's. */
  const drawLaneMarks = (ctx: CanvasRenderingContext2D, w: number, y: number,
                         marks: TimelineMark[], C: ReturnType<typeof palette>) => {
    const { viewStart, viewEnd } = p.current;
    ctx.globalAlpha = 0.9;
    for (const m of marks) {
      const sMs = m.start * 1000, eMs = (m.end ?? m.start) * 1000;
      if (eMs < viewStart || sMs > viewEnd) continue;
      ctx.fillStyle = m.row === 'event' ? m.color : C[m.row];
      const x1 = timeToPx(sMs, w);
      ctx.fillRect(x1, y, Math.max(2, timeToPx(eMs, w) - x1), 4);
    }
    ctx.globalAlpha = 1;
  };

  /** The mark under a pointer event, when it is inside the strip. */
  const markAt = (e: React.MouseEvent): { mark: TimelineMark; x: number } | null => {
    const cont = contRef.current;
    const strip = stripOf(p.current);
    if (!cont || !strip) return null;
    const rect = cont.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    const top = rect.height - strip;
    if (y < top) return null;
    // Which row the pointer is in decides which marks it can hit.
    const rows = rowsOf(p.current);
    const i = Math.floor((y - top) / PRESENCE_ROW_H);
    const row = i < rows.length ? rows[i] : 'event';
    const mark = hitMark((p.current.marks ?? []).filter(m => m.row === row), x,
                         s => timeToPx(s * 1000, rect.width), row === 'event' ? 5 : 3);
    return mark ? { mark, x } : null;
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
    const head = palette(cont).head;
    const draw = (epoch: number, top: number, bottom: number) => {
      const px = timeToPx(epoch * 1000, w);
      if (px < 0 || px > w) return;
      ctx.strokeStyle = head;
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, top); ctx.lineTo(px, bottom); ctx.stroke();
      ctx.fillStyle = head;
      ctx.beginPath();
      ctx.moveTo(px - 5, top); ctx.lineTo(px + 5, top); ctx.lineTo(px, top + 7);
      ctx.closePath(); ctx.fill();
    };
    const lanes = p.current.lanes;
    if (lanes?.some(l => l.playhead !== undefined)) {
      // Independent tiles: one head per lane, kept inside its own band.
      const laneH = h / lanes.length;
      lanes.forEach((l, i) => { if (l.playhead != null) draw(l.playhead, i * laneH, (i + 1) * laneH); });
      return;
    }
    const { playhead } = p.current;
    if (playhead != null) draw(playhead, 0, h);
  };

  /* eslint-disable react-hooks/exhaustive-deps */
  useEffect(() => { drawBase(); drawHead(); },
    [props.viewStart, props.viewEnd, props.range, props.gaps, props.lanes, props.marks,
     props.presenceRows]);
  useEffect(() => { drawHead(); }, [props.playhead, props.lanes]);

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
    const hit = p.current.onMarkClick ? markAt(e) : null;
    if (hit) { p.current.onMarkClick!(hit.mark); return; }
    const rect = cont.getBoundingClientRect();
    const { viewStart, viewEnd, onSeek } = p.current;
    const epoch = (viewStart + ((e.clientX - rect.left) / rect.width) * (viewEnd - viewStart)) / 1000;
    const slop = ((viewEnd - viewStart) / 1000 / rect.width) * 1.5; // widen tiny painted gaps
    const lanes = p.current.lanes;
    const lane = lanes?.length
      ? Math.min(lanes.length - 1, Math.max(0, Math.floor(((e.clientY - rect.top) / rect.height) * lanes.length)))
      : undefined;
    onSeek(epoch, slop, lane);
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
  const height = (lanes ? lanes.length : 1) * laneH + stripOf(props);
  const hm = hover?.mark;

  return (
    <div className="pb-timeline-wrap">
      <div ref={contRef} className="pb-timeline"
           title={hover ? undefined : 'Click to seek · drag to pan · scroll to zoom'}
           style={{ height, cursor: hover ? 'pointer' : undefined }}
           onClick={onClick} onMouseDown={onMouseDown}
           onMouseMove={e => setHover(markAt(e))} onMouseLeave={() => setHover(null)}>
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
      {hm && (
        <div className="pb-mark-tip" role="tooltip" style={{ left: hover.x }}>
          <div className="pb-mark-tip-head">
            <span className="pb-mark-dot" style={{ background: hm.color }} />{hm.label}
          </div>
          <div>
            {fmtClock(hm.start)}
            {hm.end != null && ` – ${fmtClock(hm.end)}`}
            {hm.objectClass && ` · ${hm.objectClass}`}
            {hm.zone && ` · ${hm.zone}`}
            {hm.confidence != null && ` · ${Math.round(hm.confidence * 100)}%`}
          </div>
        </div>
      )}
      <div className="pb-labels">
        {labels.map((l, i) => (
          <span key={i} style={{ position: 'absolute', left: `${l.pct}%`, transform: 'translateX(-50%)' }}>
            {l.text}
          </span>
        ))}
      </div>
    </div>
  );
}
