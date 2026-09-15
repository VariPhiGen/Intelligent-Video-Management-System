/**
 * LivePage.tsx — Live View (legacy liveInit/liveRender family): three sub-tabs
 * (Live grid / Expanded camera / Video wall), camera tree rail with zone
 * filtering, Auto/2×2/3×3/4×4 layouts, staggered HLS attach, and a
 * localStorage-persisted 3×3 video wall.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useCameras, zoneOf } from '@/lib/cameras';
import { useToast } from '@/components/Toast';
import type { Camera } from '@/lib/types';
import { Clock, LiveVideo, LvBadges } from './LiveVideo';
import { ExpandedView } from './ExpandedView';
import { WallView } from './WallView';

type View = 'grid' | 'wall';
type Layout = 'auto' | '4' | '9' | '16';

const LAYOUTS: { key: Layout; label: string }[] = [
  { key: 'auto', label: 'Auto' },
  { key: '4', label: '2×2' },
  { key: '9', label: '3×3' },
  { key: '16', label: '4×4' },
];

const HINTS = {
  grid: 'Low-latency HLS from the relay (~2–4 s behind real time). Click a tile to expand, Esc to return; offline tiles reconnect automatically.',
  focus: 'Esc returns to the grid. Camera facts are read live over ONVIF where available.',
  wall: 'Click a filled monitor to clear it. Push cameras from Live grid or an expanded camera. The wall layout persists on this workstation.',
};

// Video wall: 9 monitor slots (camera ids), persisted per browser (legacy 'vms-wall').
const WALL_KEY = 'vms-wall';
export function loadWall(): (string | null)[] {
  try {
    const v = JSON.parse(localStorage.getItem(WALL_KEY) || 'null');
    if (Array.isArray(v)) return Array.from({ length: 9 }, (_, i) => (typeof v[i] === 'string' ? v[i] : null));
  } catch { /* corrupt state — start fresh */ }
  return Array(9).fill(null);
}
function saveWall(cells: (string | null)[]) {
  try { localStorage.setItem(WALL_KEY, JSON.stringify(cells)); } catch { /* private mode */ }
}

// The narrowest a tile gets before a wide grid adds another column, and the most
// columns Auto will choose. 460px keeps a 16:10 tile legible at a glance; past
// six, tiles on a single monitor are too small to watch.
const MIN_TILE_PX = 460;
const MAX_AUTO_COLS = 6;

/** Columns for the live grid. A fixed layout is exactly what it says. Auto
 *  starts from the camera count and, on a grid wide enough, adds columns so a
 *  large monitor shows more cameras per row instead of a few enormous tiles
 *  running below the fold. `width` is the grid's own width (0 = not measured). */
function liveCols(layout: Layout, n: number, width = 0): number {
  if (layout !== 'auto') return { '4': 2, '9': 3, '16': 4 }[layout] ?? 3;
  const byCount = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  const byWidth = Math.min(MAX_AUTO_COLS, Math.floor(width / MIN_TILE_PX));
  return Math.max(1, Math.min(n, Math.max(byCount, byWidth)));
}

/** The element's content width, kept current as the window or rail resizes.
 *  A callback ref, not a mount effect: the grid only renders once cameras have
 *  loaded, so an effect that ran at mount would find nothing to observe. */
function useWidth<T extends HTMLElement>() {
  const [width, setWidth] = useState(0);
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: T | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.round(entry.contentRect.width)));
    ro.observe(el);
    observer.current = ro;
  }, []);
  useEffect(() => () => observer.current?.disconnect(), []);
  return [ref, width] as const;
}

function RailNode({ label, count, active, dot, pad, onClick }: {
  label: string; count?: number; active: boolean; dot?: string; pad: number; onClick: () => void;
}) {
  return (
    <div className={`lv-node${active ? ' active' : ''}`} style={{ paddingLeft: pad }} onClick={onClick}>
      {dot && <span style={{ width: 7, height: 7, borderRadius: '50%', background: dot, flexShrink: 0 }} />}
      <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
      {count != null && (
        <span style={{ marginLeft: 'auto', fontFamily: 'var(--mono)', fontSize: 9, color: 'var(--dim)' }}>{count}</span>
      )}
    </div>
  );
}

export function LivePage() {
  const toast = useToast();
  const { cameras, uptime, loading } = useCameras();
  const [view, setView] = useState<View>('grid');
  const [focus, setFocus] = useState<string | null>(null);
  const [zone, setZone] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>('auto');
  const [filter, setFilter] = useState('');
  const [wallCells, setWallCells] = useState<(string | null)[]>(loadWall);
  const [gridRef, gridWidth] = useWidth<HTMLDivElement>();

  const enabled = useMemo(() => cameras.filter(c => c.enabled), [cameras]);
  const zoneCams = useMemo(
    () => (zone ? enabled.filter(c => zoneOf(c) === zone) : enabled),
    [enabled, zone],
  );

  // Deep link: /live?cam=<id> opens the expanded view (wizard/config link here).
  useEffect(() => {
    const camParam = new URLSearchParams(location.hash.split('?')[1] || '').get('cam');
    if (camParam && cameras.some(c => c.id === camParam)) {
      setView('grid');
      setFocus(camParam);
    }
    // Consume once when cameras first arrive.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameras.length > 0]);

  const focusCam = focus ? cameras.find(c => c.id === focus) ?? null : null;

  // ── Sub-tabs (legacy liveTabGrid/liveTabFocus/liveTabWall) ────────────────
  const tabGrid = () => { setView('grid'); setFocus(null); };
  const tabFocus = () => {
    setView('grid');
    if (!focus) {
      if (!zoneCams.length) { toast('No cameras in this view', 'err'); return; }
      setFocus(zoneCams[0].id);
    }
  };
  const tabWall = () => { setView('wall'); setFocus(null); };

  const setCells = (cells: (string | null)[]) => { setWallCells(cells); saveWall(cells); };

  // ── Wall actions (legacy livePushWall/wallClear/wallCellClick) ────────────
  function pushWall(id?: string) {
    const cam = cameras.find(c => c.id === (id || focus)) || zoneCams[0];
    if (!cam) { toast('No camera to push', 'err'); return; }
    const existing = wallCells.indexOf(cam.id);
    if (existing >= 0) { // duplicate video ids would break stream attach
      toast(`${cam.name} is already on Monitor ${existing + 1}`);
      tabWall();
      return;
    }
    let idx = wallCells.findIndex(c => !c);
    if (idx < 0) idx = 0;
    const next = [...wallCells];
    next[idx] = cam.id;
    setCells(next);
    toast(`Pushed ${cam.name} to Monitor ${idx + 1}`);
    tabWall();
  }
  function wallCellClick(i: number) {
    if (wallCells[i]) {
      const next = [...wallCells];
      next[i] = null;
      setCells(next);
      toast(`Cleared Monitor ${i + 1}`);
    } else {
      toast(`Monitor ${i + 1} is free — push a camera from Live grid`);
    }
  }
  const wallClear = () => setCells(Array(9).fill(null));

  // First wall visit (nothing ever pushed): seed with the fleet; also drop
  // cells whose camera was deleted/disabled since last save.
  useEffect(() => {
    if (view !== 'wall' || !cameras.length) return;
    let next = wallCells;
    if (next.every(c => !c)) {
      next = Array(9).fill(null);
      zoneCams.slice(0, 9).forEach((c, i) => { next[i] = c.id; });
    }
    next = next.map(cid => (cid && cameras.some(x => x.id === cid && x.enabled) ? cid : null));
    if (JSON.stringify(next) !== JSON.stringify(wallCells)) setCells(next);
  }, [view, cameras, zoneCams, wallCells]); // eslint-disable-line react-hooks/exhaustive-deps

  // Esc returns from the expanded camera to the grid.
  useEffect(() => {
    if (!focus) return;
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') setFocus(null); };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [focus]);

  // Focused camera deleted/disabled since — fall back to the grid.
  useEffect(() => {
    if (focus && cameras.length && !cameras.some(c => c.id === focus)) setFocus(null);
  }, [focus, cameras]);

  // ── Camera tree rail (legacy liveRailRender) ──────────────────────────────
  const q = filter.toLowerCase();
  const railCams = useMemo(
    () => enabled.filter(c => !q || c.name.toLowerCase().includes(q) || c.slug.includes(q)),
    [enabled, q],
  );
  const railZones = useMemo(() => {
    const g: Record<string, Camera[]> = {};
    railCams.forEach(c => { (g[zoneOf(c)] = g[zoneOf(c)] || []).push(c); });
    return g;
  }, [railCams]);

  const shown = layout === 'auto' ? zoneCams : zoneCams.slice(0, +layout);
  const hint = view === 'wall' ? HINTS.wall : focusCam ? HINTS.focus : HINTS.grid;
  const wallResolved = wallCells.map(cid => (cid && cameras.find(x => x.id === cid && x.enabled)) || null);

  return (
    <div className="fade">
      <div className="tabs page-tabs">
        <div className={`tab${view === 'grid' && !focusCam ? ' active' : ''}`} onClick={tabGrid}>Live grid</div>
        <div className={`tab${view === 'grid' && focusCam ? ' active' : ''}`} onClick={tabFocus}>Expanded camera</div>
        <div className={`tab${view === 'wall' ? ' active' : ''}`} onClick={tabWall}>Video wall</div>
      </div>

      <div className={`lv-wrap${view === 'wall' ? ' no-rail' : ''}`}>
        <aside className="lv-rail">
          <div className="inv-search" style={{ maxWidth: 'none', marginBottom: 'var(--s4)' }}>
            <span className="ico">⌕</span>
            <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter cameras…" />
          </div>
          <div className="label" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 'var(--s2)' }}>
            <span>Cameras</span><span>{enabled.length}</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 1, fontSize: 12.5 }}>
            <RailNode label="All cameras" count={railCams.length} pad={6}
                      active={zone === null && view === 'grid' && !focus}
                      onClick={() => { setZone(null); setFocus(null); setView('grid'); }} />
            {Object.keys(railZones).sort().map(z => (
              <div key={z} style={{ display: 'contents' }}>
                <RailNode label={z} count={railZones[z].length} pad={6} dot="var(--accent)" active={zone === z}
                          onClick={() => { setZone(z); setFocus(null); setView('grid'); }} />
                {railZones[z].map(c => (
                  <RailNode key={c.id} label={c.name} pad={22} active={focus === c.id}
                            dot={c.health_status === 'connected' ? 'var(--green)' : 'var(--red)'}
                            onClick={() => { setView('grid'); setFocus(f => (f === c.id ? null : c.id)); }} />
                ))}
              </div>
            ))}
          </div>
        </aside>

        <div className="lv-main">
          {/* One toolbar line: what you're looking at (left) · how it's laid out
              and what you can do with it (right). */}
          {view === 'grid' && !focusCam && (
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                          gap: 'var(--s3)', flexWrap: 'wrap', marginBottom: 'var(--s4)' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                <span className="live-dot" />
                <span style={{ fontSize: 15, fontWeight: 650, letterSpacing: '-.02em' }}>{zone || 'All cameras'}</span>
                <span className="cfg-count">{zoneCams.length}</span>
              </div>
              <div style={{ display: 'flex', gap: 'var(--s2)', alignItems: 'center', flexWrap: 'wrap' }}>
                <div style={{ display: 'flex', gap: 4 }}>
                  {LAYOUTS.map(l => (
                    <button key={l.key} className={`lv-ly${layout === l.key ? ' active' : ''}`}
                            onClick={() => setLayout(l.key)}>
                      {l.label}
                    </button>
                  ))}
                </div>
                <button className="btn-ghost btn-sm" onClick={() => pushWall()}>Push to wall</button>
              </div>
            </div>
          )}

          {view === 'wall' ? (
            <WallView cells={wallResolved} onCellClick={wallCellClick} onClear={wallClear} onPushFromLive={tabGrid} />
          ) : focusCam ? (
            <ExpandedView cam={focusCam} uptime={uptime[focusCam.id]} onPushWall={() => pushWall(focusCam.id)} />
          ) : !zoneCams.length ? (
            !loading && (
              <div className="emptystate">
                <div className="glyph">▦</div>
                <h4>Nothing to watch here</h4>
                <p>{zone
                  ? `No enabled cameras in ${zone}. Pick another zone, or enable a camera in this one.`
                  : 'No cameras are enabled yet. Add one, or enable an existing camera to start streaming.'}</p>
                {zone
                  ? <button className="btn-ghost btn-sm" onClick={() => setZone(null)}>Show all cameras</button>
                  : <a className="btn-primary btn-sm" href="#/cameras/add" style={{ color: 'var(--onAccent)' }}>＋ Add camera</a>}
              </div>
            )
          ) : (
            <div ref={gridRef} style={{ display: 'grid', gap: 12, gridTemplateColumns: `repeat(${liveCols(layout, shown.length, gridWidth)}, 1fr)` }}>
              {shown.map((c, i) => (
                <div key={c.id} className="lv-tile" title="Expand" onClick={() => setFocus(c.id)}>
                  <LiveVideo slug={c.slug} variant="tile" staggerMs={i * 250}
                              hasSub={!!c.sub_track?.url_raw} />
                  <LvBadges c={c} />
                  <div style={{ position: 'absolute', left: 9, bottom: 8, pointerEvents: 'none' }}>
                    <div style={{ fontSize: 11.5, fontWeight: 500, color: '#fff', textShadow: '0 1px 3px rgba(0,0,0,.7)' }}>
                      {c.name}
                    </div>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: '#DCE3EC', textShadow: '0 1px 3px rgba(0,0,0,.7)' }}>
                      {c.slug} · <Clock />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="d-hint" style={{ marginTop: 12 }}>{hint}</div>
        </div>
      </div>
    </div>
  );
}
