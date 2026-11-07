/**
 * WallView.tsx — the video wall: any number of cameras (1 … all) auto-arranged to use the whole
 * stage, with the client's logo + title in the header.
 *
 *  • Layout: bestWallGrid() picks the column count that makes N 16:9 tiles as large as possible for
 *    the stage's CURRENT size, re-fitted on resize and on entering/leaving fullscreen. Outside
 *    fullscreen the stage takes the remaining viewport height, so the wall fills the screen either way.
 *  • Click a camera → it fills the wall (main stream, low-latency, like the expanded view); click again
 *    → back to the wall. Removing a camera is the ✕ that appears on hover — a click is for looking.
 *  • Tiles use the cameras' sub (low-res) stream where one exists, so a dense wall stays smooth.
 *  • Offline cameras leave the grid (it re-fits to the cameras that ARE live) instead of sitting there
 *    as black tiles; the header says "12 / 14 live" and the "2 offline" chip lists which. A camera
 *    must be down for GRACE_MS before it's dropped (one missed poll mustn't re-flow a 14-tile wall)
 *    and comes back the moment it streams. Opt out per screen in ⚙ → "Hide offline cameras".
 *  • Display settings (wallPrefs.ts — admin default + this browser's overrides): top padding for TVs
 *    whose overlay covers the top edge, a bigger logo/title for walls read from across a room, and a
 *    per-theme background + title colour. The background covers the WHOLE wall — padding strip,
 *    header and the bars beside a narrower grid — so it reads as one surface, not a framed box.
 */
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import type { Camera } from '@/lib/types';
import { Clock, LiveVideo, LvBadges } from './LiveVideo';
import { bestWallGrid, moveWallCamera } from './wallLayout';
import { autoReload } from '@/components/AppErrorBoundary';
import { usePtzPositions } from '@/lib/ptz';
import { splitWall, themed, trackDown, type Theme, type WallPrefs } from './wallPrefs';

const GAP = 6;
const GRACE_MS = 20_000;   // > one camera-list poll (15 s): a single missed poll never drops a tile
const PAD = 8;   // .wall-stage padding (px) — the grid is fitted to the stage's INNER box
const ORDER_KEY = 'vms-wall-order';
const DRAG_TYPE = 'application/x-vms-wall-camera';

export function WallView({ cams, prefs, theme, customised, title, logoUrl, onRemove, onClear, onPick, onSettings }: {
  cams: Camera[];
  prefs: WallPrefs;
  theme: Theme;
  customised: boolean;
  title: string;
  logoUrl: string | null;
  onRemove: (id: string) => void;
  onClear: () => void;
  onPick: () => void;
  onSettings: () => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 1200, h: 640 });
  const [spot, setSpot] = useState<string | null>(null);
  const [fs, setFs] = useState(false);
  const [showOffline, setShowOffline] = useState(false);
  const [order, setOrder] = useState<string[]>(() => {
    try {
      const saved: unknown = JSON.parse(localStorage.getItem(ORDER_KEY) || '[]');
      return Array.isArray(saved) ? saved.filter((id): id is string => typeof id === 'string') : [];
    } catch { return []; }
  });
  const [dragged, setDragged] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);
  const dragEnded = useRef(0);
  const orderedIds = [...new Set([...order.filter(id => cams.some(c => c.id === id)), ...cams.map(c => c.id)])];
  const orderedCams = orderedIds.map(id => cams.find(c => c.id === id)!);
  useEffect(() => {
    try {
      if (order.length) localStorage.setItem(ORDER_KEY, JSON.stringify(order));
      else localStorage.removeItem(ORDER_KEY);
    } catch { /* Arrangement still works when browser storage is unavailable. */ }
  }, [order]);
  function move(source: string, target: string) {
    setOrder(moveWallCamera(orderedIds, source, target));
  }

  // An unattended wall: keep the screen on (Screen Wake Lock — released by the browser whenever the page is hidden,
  // so it's taken again on every return), and reload the page every 6 h as a safety net for whatever a browser /
  // Android WebView accumulates over days. The session survives a reload (SSO cookie; "Remember this workstation"
  // keeps it for 30 days), and the Live page reopens on the wall (it remembers its tab).
  useEffect(() => {
    let lock: { release: () => Promise<void> } | null = null;
    const take = () => {
      const wl = (navigator as any).wakeLock;
      if (document.visibilityState === 'visible' && wl?.request) wl.request('screen').then((l: any) => { lock = l; }).catch(() => {});
    };
    take();
    document.addEventListener('visibilitychange', take);
    const reload = window.setTimeout(() => autoReload('scheduled wall refresh (6 h)'), 6 * 3600 * 1000);
    return () => {
      document.removeEventListener('visibilitychange', take);
      window.clearTimeout(reload);
      lock?.release().catch(() => {});
    };
  }, []);

  // Down-since bookkeeping for the offline hysteresis. A ref (not state): it changes on every poll
  // and only matters when the wall re-renders, which the cams update + the 5 s tick already cause.
  const downSince = useRef(new Map<string, number>());
  const firstLoad = useRef(true);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!cams.length) return;
    trackDown(downSince.current, cams, Date.now(), firstLoad.current);
    firstLoad.current = false;
    setNow(Date.now());
  }, [cams]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(t);
  }, []);
  const { shown, offline, liveCount, allDown } = splitWall(orderedCams, downSince.current, now, GRACE_MS, prefs.hideOffline);

  // A spotlighted camera that left the wall (removed / disabled / went offline) drops back to the grid.
  useEffect(() => { if (spot && !shown.some(c => c.id === spot)) setSpot(null); }, [shown, spot]);
  // Esc leaves the spotlight first (fullscreen's own Esc is handled by the browser).
  useEffect(() => {
    if (!spot) return;
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') setSpot(null); };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [spot]);

  useLayoutEffect(() => {
    const measure = () => {
      const el = stageRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const inFs = document.fullscreenElement === rootRef.current;
      setFs(inFs);
      // Fill what's left of the viewport below the stage's top (minus its own padding + a margin).
      const h = Math.max(200, window.innerHeight - rect.top - (inFs ? 12 : 64));
      setSize({ w: Math.max(200, el.clientWidth - 2 * PAD), h });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (stageRef.current) ro.observe(stageRef.current);
    window.addEventListener('resize', measure);
    document.addEventListener('fullscreenchange', measure);
    return () => { ro.disconnect(); window.removeEventListener('resize', measure);
                   document.removeEventListener('fullscreenchange', measure); };
    // The header's size is a setting now — re-fit when it changes, not only on resize.
  }, [prefs.topPad, prefs.logoH, prefs.titlePx]);

  const spotCam = spot ? shown.find(c => c.id === spot) : null;
  const ptzPos = usePtzPositions(shown.filter(c => c.ptz_capable).map(c => c.id));
  const grid = bestWallGrid(shown.length, size.w, size.h - 2 * PAD, GAP);
  const subPx = Math.max(11.5, Math.round(prefs.titlePx * 0.45));
  const { bg, titleColor } = themed(prefs, theme);
  // With a custom background the root becomes the surface (rounded, padded like the stage) and the
  // stage goes transparent, so padding + header + side bars are one colour.
  const rootStyle: CSSProperties = {
    ...(prefs.topPad ? { paddingTop: prefs.topPad } : {}),
    ...(bg ? { background: bg, borderRadius: fs ? 0 : 'var(--r-md)', paddingLeft: 10, paddingRight: 10,
               paddingBottom: 10, ...(prefs.topPad ? {} : { paddingTop: 10 }) } : {}),
  };

  return (
    /* id reuses the #live-grid:fullscreen styling from global.css */
    <div id="live-grid" ref={rootRef} style={rootStyle}>
      <div className="wall-head">
        {logoUrl && <img className="wall-logo" src={logoUrl} alt=""
                         style={{ height: prefs.logoH, maxWidth: prefs.logoH * 5 }} />}
        <div style={{ minWidth: 0 }}>
          <div className="wall-title" style={{ fontSize: prefs.titlePx, lineHeight: 1.15, ...(titleColor ? { color: titleColor } : {}) }}>{title}</div>
          <div style={{ fontSize: subPx, color: 'var(--muted)', marginTop: 2 }}>
            {!cams.length ? 'No cameras selected'
              : `${prefs.followAll ? 'All cameras' : `${cams.length} camera${cams.length === 1 ? '' : 's'}`}`
                + (shown.length ? ` · ${grid.cols}×${grid.rows} auto layout` : '')}
          </div>
        </div>
        {cams.length > 0 && (
          <span className="wall-pill" style={{ fontSize: Math.max(11, Math.round(prefs.titlePx * 0.6)) }}
                title={allDown ? 'No camera on the wall is reporting a live stream — showing all tiles' : undefined}>
            <span className="wall-blip" style={liveCount ? undefined : { background: 'var(--red)' }} />
            {liveCount} / {cams.length} live
          </span>
        )}
        {offline.length > 0 && (
          <div style={{ position: 'relative' }}>
            <button className="wall-off-chip" onClick={() => setShowOffline(v => !v)} aria-expanded={showOffline}>
              {offline.length} offline ▾
            </button>
            {showOffline && (
              <div className="wall-off-list" onMouseLeave={() => setShowOffline(false)}>
                <div className="label" style={{ marginBottom: 6 }}>
                  {prefs.hideOffline && !allDown ? 'Not streaming — hidden from the wall' : 'Not streaming'}
                </div>
                {offline.map(c => (
                  <div key={c.id} className="wall-off-row">
                    <span className="d" /> <span style={{ flex: 1 }}>{c.name}</span>
                    <span style={{ color: 'var(--dim)', fontFamily: 'var(--mono)', fontSize: 10.5 }}>{c.ip || c.slug}</span>
                  </div>
                ))}
                <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 6 }}>They return to the wall as soon as they stream again.</div>
              </div>
            )}
          </div>
        )}
        <div style={{ flex: 1 }} />
        {spotCam && <button className="btn-primary btn-sm" onClick={() => setSpot(null)}>Show all</button>}
        {!spotCam && order.length > 0 && <button className="btn-ghost btn-sm" onClick={() => setOrder([])}>Reset order</button>}
        <button className="btn-ghost btn-sm" onClick={onPick}>＋ Select cameras</button>
        {cams.length > 0 && <button className="btn-ghost btn-sm" onClick={onClear}>Clear wall</button>}
        <button className="btn-primary btn-sm"
                onClick={() => (fs ? document.exitFullscreen?.() : rootRef.current?.requestFullscreen?.())}>
          {fs ? 'Exit fullscreen' : 'Fullscreen'}
        </button>
        {customised && (
          <span className="wall-custom" title="This screen overrides some of the admin's default wall settings — ⚙ to see or reset them">
            customised
          </span>
        )}
        <button className="btn-ghost btn-sm" onClick={onSettings}
                title="Wall settings — offline cameras, top padding, header size (and the client logo/title for admins)"
                aria-label="Wall settings">⚙</button>
      </div>

      <div className="wall-stage" ref={stageRef}
           style={{ height: size.h, padding: PAD, ...(bg ? { background: 'transparent', border: 'none' } : {}) }}>
        {!cams.length ? (
          <div className="wall-none">
            <div style={{ fontSize: 13, marginBottom: 10 }}>The wall is empty.</div>
            <button className="btn-primary btn-sm" onClick={onPick}>Select cameras</button>
          </div>
        ) : !shown.length ? (
          <div className="wall-none">
            <div style={{ fontSize: 13 }}>None of the {cams.length} cameras on this wall is streaming right now.</div>
          </div>
        ) : spotCam ? (
          <div className="wall-cell wall-spot" onClick={() => setSpot(null)} title="Click to return to the wall">
            <LiveVideo slug={spotCam.slug} variant="focus" hasSub={!!spotCam.sub_track?.url_raw} />
            <LvBadges c={spotCam} pos={ptzPos[spotCam.id]} />
            <div className="wall-caption" style={{ fontSize: 13 }}>
              <div style={{ fontWeight: 600 }}>{spotCam.name}</div>
              <div className="wall-sub">{spotCam.slug} · <Clock /> · click to return to the wall</div>
            </div>
          </div>
        ) : (
          <div style={{ display: 'grid', gap: GAP, justifyContent: 'center', alignContent: 'center', height: '100%',
                        gridTemplateColumns: `repeat(${grid.cols}, ${grid.tileW}px)`,
                        gridAutoRows: `${grid.tileH}px` }}>
            {shown.map((c, i) => (
              <div key={c.id} className="wall-cell" draggable
                onClick={() => { if (Date.now() - dragEnded.current > 250) setSpot(c.id); }}
                title="Click to enlarge · drag to rearrange"
                style={{ opacity: dragged === c.id ? 0.5 : 1,
                  ...(dropTarget === c.id && dragged !== c.id ? { outline: '2px solid var(--accent)', outlineOffset: 2 } : {}) }}
                onDragStart={e => {
                  e.dataTransfer.setData(DRAG_TYPE, c.id);
                  e.dataTransfer.effectAllowed = 'move';
                  setDragged(c.id);
                }}
                onDragOver={e => {
                  if (!dragged || dragged === c.id) return;
                  e.preventDefault();
                  e.dataTransfer.dropEffect = 'move';
                  setDropTarget(c.id);
                }}
                onDragLeave={e => {
                  if (!(e.relatedTarget instanceof Node) || !e.currentTarget.contains(e.relatedTarget)) setDropTarget(null);
                }}
                onDrop={e => {
                  e.preventDefault();
                  const source = e.dataTransfer.getData(DRAG_TYPE);
                  if (source === dragged) move(source, c.id);
                  setDragged(null); setDropTarget(null); dragEnded.current = Date.now();
                }}
                onDragEnd={() => { setDragged(null); setDropTarget(null); dragEnded.current = Date.now(); }}>
                <LiveVideo slug={c.slug} variant="wall" preferSub staggerMs={i * 250} hasSub={!!c.sub_track?.url_raw} />
                <LvBadges c={c} pos={ptzPos[c.id]} />
                <button className="wall-x" title="Remove from wall" aria-label={`Remove ${c.name} from wall`}
                        onClick={e => { e.stopPropagation(); onRemove(c.id); }}>✕</button>
                <button className="btn-ghost btn-sm" style={{ position: 'absolute', left: 6, top: 6, zIndex: 3, cursor: 'grab', background: 'rgba(5,7,12,.75)', color: '#fff' }}
                    aria-label={`Reorder ${c.name}`} title="Drag to rearrange, or use arrow keys while focused"
                    onClick={e => e.stopPropagation()}
                    onKeyDown={e => {
                      const step = e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1
                        : e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : 0;
                      if (!step) return;
                      e.preventDefault(); e.stopPropagation();
                      const target = shown[i + step];
                      if (target) move(c.id, target.id);
                    }}>⠿</button>
                <div className="wall-caption">
                  <div style={{ fontWeight: 500 }}>{c.name}</div>
                  {grid.tileH >= 120 && <div className="wall-sub">{c.slug} · <Clock /></div>}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
