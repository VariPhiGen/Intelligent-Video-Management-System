/**
 * LivePage.tsx — Live View: three sub-tabs (Live grid / Expanded camera /
 * Video wall), camera tree rail with zone filtering, Auto/2×2/3×3/4×4 layouts,
 * a PAGED grid, staggered HLS attach, and a video wall (any number of cameras,
 * auto-arranged) whose settings are an admin default + per-browser overrides.
 *
 * PAGING IS NOT A CONVENIENCE HERE (the Live grid). A fixed layout used to render
 * `zoneCams.slice(0, +layout)` — on a hundred-camera site "3×3" showed the
 * first nine and the other ninety-one were unreachable from this page, with
 * nothing on screen saying so. Auto had the opposite failure: it rendered every
 * camera, attaching a hundred HLS sessions at 250 ms intervals, which is a
 * twenty-five second ramp onto a relay serving a hundred streams. A page is
 * both the layout and the decode budget, and the pager says which slice of the
 * fleet is on screen.
 *
 * MERGE NOTE (field -> ai, 2026-10-07): the ai line's paged 9-monitor
 * wall was replaced by field's auto-layout wall (any count, offline cameras
 * step aside, admin default + per-screen overrides, client branding) — that
 * wall uses each camera's sub-stream on tiles, which is what keeps a dense wall
 * inside the decode budget the paged design was protecting.
 */
import { ViewTransition } from '@/components/ViewTransition';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useCameras, zoneOf } from '@/lib/cameras';
import { useToast } from '@/components/Toast';
import { CameraFilterBar } from '@/components/CameraFilterBar';
import { applyFilter, loadFilter, saveFilter, type CameraFilter } from '@/lib/cameraFilters';
import type { Camera } from '@/lib/types';
import { Clock, LiveVideo, LvBadges } from './LiveVideo';
import { ExpandedView } from './ExpandedView';
import { WallView } from './WallView';
import { WallPicker } from './WallPicker';
import { WallSettings } from './WallSettings';
import { allWallCameras, effectivePrefs, loadLocal, normalizePrefs, saveLocal, withOverride, withoutOverride,
         type WallKey, type WallPrefs } from './wallPrefs';
import { DEFAULT_WALL_TITLE, logoFor, useBranding, useDocTheme } from '@/lib/branding';
import { apiFetch } from '@/lib/api';
import { usePtzPositions } from '@/lib/ptz';
import { useAuth } from '@/lib/auth';

type View = 'grid' | 'wall';
type Layout = 'auto' | '4' | '9' | '16';

const LAYOUTS: { key: Layout; label: string }[] = [
  { key: 'auto', label: 'Auto' },
  { key: '4', label: '2×2' },
  { key: '9', label: '3×3' },
  { key: '16', label: '4×4' },
];

const HINTS = {
  grid: 'Low-latency HLS from the relay (~2–4 s behind real time). Click a tile to expand, Esc to return; ← and → page through the cameras. Offline tiles reconnect automatically.',
  focus: 'Esc returns to the grid. Camera facts are read live over ONVIF where available.',
  wall: 'Click a camera to enlarge it, then Show all to return. Drag tiles to rearrange them on this browser, or focus a reorder handle and use the arrow keys. Hover a camera and press ✕ to remove it. Offline cameras step aside and come back when they stream again. An admin sets the default wall in ⚙.',
};

// Video wall settings (cameras, offline handling, header, colours) = the admin's default on the
// server overlaid with this browser's per-field overrides — see wallPrefs.ts.

// The narrowest a tile gets before a wide grid adds another column, and the most
// columns Auto will choose. 460px keeps a 16:10 tile legible at a glance; past
// six, tiles on a single monitor are too small to watch.
const MIN_TILE_PX = 460;
const MAX_AUTO_COLS = 6;

// The narrowest Auto will ever make a tile, whatever the camera count asks
// for. Seven cameras asked for three columns even on a 330px phone, and the
// tiles came out ~100px wide with the LIVE/REC badges printed over the camera
// name. The fixed layouts still mean exactly what they say.
const MIN_AUTO_TILE_PX = 240;
const GRID_GAP_PX = 12;

// The most tiles Auto will put on one page. Matches the largest fixed layout
// on purpose: 4×4 is the point past which a tile stops being watchable, and
// Auto should not quietly exceed the ceiling the explicit choices respect.
const AUTO_MAX_TILES = 16;

/** Above this many tiles on one page, a tile is too small to be worth decoding
 *  a main stream for. Nine is where the video wall used to sit, for exactly
 *  this reason. */
export const LOW_RES_ABOVE = 9;

/** Tiles per page. A fixed layout IS its page size. Auto fills whole rows only
 *  — a ragged last row on a paged grid reads as missing cameras. */
export function pageSize(layout: Layout, cols: number): number {
  if (layout !== 'auto') return +layout;
  return Math.max(cols, cols * Math.floor(AUTO_MAX_TILES / cols));
}

/** Columns for the live grid. A fixed layout is exactly what it says. Auto
 *  starts from the camera count and, on a grid wide enough, adds columns so a
 *  large monitor shows more cameras per row instead of a few enormous tiles
 *  running below the fold. `width` is the grid's own width (0 = not measured). */
export function liveCols(layout: Layout, n: number, width = 0): number {
  if (layout !== 'auto') return { '4': 2, '9': 3, '16': 4 }[layout] ?? 3;
  const byCount = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  const byWidth = Math.min(MAX_AUTO_COLS, Math.floor(width / MIN_TILE_PX));
  // Unmeasured (0) is not "narrow": leave the count to decide until the
  // ResizeObserver reports.
  const fits = width > 0
    ? Math.floor((width + GRID_GAP_PX) / (MIN_AUTO_TILE_PX + GRID_GAP_PX))
    : Infinity;
  return Math.max(1, Math.min(n, fits, Math.max(byCount, byWidth)));
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
  // The tab survives a reload: an unattended video wall that reloads (by itself after an error, or a scheduled
  // refresh) must come back on the WALL, not on the grid. #/live?view=wall pins it for a kiosk URL; otherwise the
  // last tab used in this browser.
  const [view, setView] = useState<View>(() => {
    const q = new URLSearchParams(location.hash.split('?')[1] || '').get('view');
    if (q === 'wall' || q === 'grid') return q;
    try { return localStorage.getItem('vms-live-view') === 'wall' ? 'wall' : 'grid'; } catch { return 'grid'; }
  });
  useEffect(() => { try { localStorage.setItem('vms-live-view', view); } catch { /* storage off */ } }, [view]);
  const [focus, setFocus] = useState<string | null>(null);
  const [zone, setZone] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>('auto');
  const [filter, setFilter] = useState('');
  // Filter chips (status / PTZ / maker / recording / no ONVIF login), remembered in this browser.
  const [cf, setCfState] = useState<CameraFilter>(() => loadFilter('live'));
  const setCf = (f: CameraFilter) => { setCfState(f); saveFilter('live', f); };
  const [page, setPage] = useState(0);
  const [gridRef, gridWidth] = useWidth<HTMLDivElement>();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const { isAdmin } = useAuth();
  const { branding, loaded: brandingLoaded, logoLightUrl, logoDarkUrl, refresh: refreshBranding } = useBranding();
  const theme = useDocTheme();

  // ── Wall settings: admin default (server) + this browser's overrides ───────────────────────────
  const [local, setLocalState] = useState<Partial<WallPrefs>>(loadLocal);
  const setLocal = (p: Partial<WallPrefs>) => { setLocalState(p); saveLocal(p); };
  const defaults = useMemo(() => normalizePrefs(branding.wall_defaults), [branding.wall_defaults]);
  const wallPrefs = useMemo(() => effectivePrefs(branding.wall_defaults, local), [branding.wall_defaults, local]);
  const override = (patch: Partial<WallPrefs>) => setLocal(withOverride(local, defaults, patch));
  const resetLocal = (keys?: WallKey[]) => setLocal(keys ? withoutOverride(local, keys) : {});
  // Admin: make (part of) what this screen shows the default for everyone, then stop overriding it
  // here — this browser goes back to following the (new) default like every other screen.
  async function publishDefaults(patch: Partial<WallPrefs>, keys: WallKey[]) {
    const next = { ...defaults, ...patch };
    await apiFetch('/branding', { method: 'PUT', body: JSON.stringify({ wall_defaults: next }) });
    setLocal(withoutOverride(local, keys));
    refreshBranding();
  }

  const enabled = useMemo(() => cameras.filter(c => c.enabled), [cameras]);
  // The chips and the text box narrow BOTH the rail and the grid — it used to be the rail only, so a search found a
  // camera on the left while the grid kept paging through all of them.
  const q = filter.toLowerCase();
  const filtered = useMemo(
    () => applyFilter(enabled, cf).filter(c => !q || c.name.toLowerCase().includes(q) || c.slug.includes(q)),
    [enabled, cf, q],
  );
  const zoneCams = useMemo(
    () => (zone ? filtered.filter(c => zoneOf(c) === zone) : filtered),
    [filtered, zone],
  );

  // Columns come from the WHOLE set and the measured width, then the page size
  // follows from the columns — so changing zone does not reflow the grid.
  const cols = liveCols(layout, zoneCams.length, gridWidth);
  const per = pageSize(layout, cols);
  const pages = Math.max(1, Math.ceil(zoneCams.length / per));
  // Clamp rather than reset: a narrowed filter that leaves fewer pages should
  // land on the last one, not throw the operator back to the first.
  const safePage = Math.min(page, pages - 1);
  const from = safePage * per;
  const shown = zoneCams.slice(from, from + per);
  // Where each PTZ camera on screen points (one request for the page, every 5 s).
  const ptzPos = usePtzPositions(shown.filter(c => c.ptz_capable).map(c => c.id));

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

  // Camera changes made on the wall itself (Push, ✕, Clear) are this browser's override; an admin
  // makes them everyone's via ⚙ → "Save as default for everyone" or the picker's checkbox.
  const setWall = (ids: string[]) => override({ followAll: false, cameraIds: ids });
  const wallIds = wallPrefs.followAll ? allWallCameras(cameras, zoneOf).map(c => c.id) : wallPrefs.cameraIds;

  // ── Wall actions ───────────────────────────────────────────────────────────
  function pushWall(id?: string) {
    const cam = cameras.find(c => c.id === (id || focus)) || zoneCams[0];
    if (!cam) { toast('No camera to push', 'err'); return; }
    const ids = wallIds;
    if (ids.includes(cam.id)) { // duplicate video ids would break stream attach
      toast(`${cam.name} is already on the wall`);
    } else {
      setWall([...ids, cam.id]);
      toast(`Added ${cam.name} to the wall`);
    }
    tabWall();
  }
  const wallRemove = (id: string) => {
    // ✕ on an "all cameras" wall turns it into a fixed list (everything but that one) — the operator
    // asked for this camera to go, so following-all would just bring it straight back.
    if (wallPrefs.followAll) {
      setWall(wallIds.filter(x => x !== id));
      toast('Wall switched from “All cameras” to a fixed selection');
      return;
    }
    setWall(wallIds.filter(x => x !== id));
  };
  const wallClear = () => setWall([]);

  // A different set of cameras is a different fleet to walk: page 1 again.
  useEffect(() => { setPage(0); }, [zone, layout]);
  useEffect(() => { if (page !== safePage) setPage(safePage); }, [page, safePage]);

  // ← / → page the grid. The arrows are free here — the grid has no caret and
  // no horizontal scroll — and paging a hundred cameras by mouse is a chore.
  // Ignored while typing, while a camera is expanded, and on the video wall (it
  // auto-fits every selected camera; there are no wall pages to walk).
  useEffect(() => {
    if (focus || view === 'wall' || pickerOpen || settingsOpen) return;
    const last = pages - 1;
    if (last < 1) return;
    const h = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      if (e.key === 'ArrowRight') setPage(p => Math.min(last, p + 1));
      else if (e.key === 'ArrowLeft') setPage(p => Math.max(0, p - 1));
    };
    document.addEventListener('keydown', h);
    return () => document.removeEventListener('keydown', h);
  }, [view, focus, pickerOpen, settingsOpen, pages]);

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
  const railCams = filtered;
  const railZones = useMemo(() => {
    const g: Record<string, Camera[]> = {};
    railCams.forEach(c => { (g[zoneOf(c)] = g[zoneOf(c)] || []).push(c); });
    return g;
  }, [railCams]);

  const hint = view === 'wall' ? HINTS.wall : focusCam ? HINTS.focus : HINTS.grid;
  // Deleted/disabled cameras simply drop out here (the stored list may still name them).
  const wallCams = !brandingLoaded ? [] : wallPrefs.followAll
    ? allWallCameras(cameras, zoneOf)
    : wallPrefs.cameraIds
      .map(cid => cameras.find(x => x.id === cid && x.enabled))
      .filter((c): c is Camera => !!c);
  const logoUrl = logoFor(theme, logoLightUrl, logoDarkUrl);

  return (
    <ViewTransition view={`${view}:${!!focusCam}`}>
      <div className="tabs page-tabs">
        <button type="button" aria-pressed={view === 'grid' && !focusCam} className={`tab${view === 'grid' && !focusCam ? ' active' : ''}`} onClick={tabGrid}>Live grid</button>
        <button type="button" aria-pressed={view === 'grid' && !!focusCam} className={`tab${view === 'grid' && focusCam ? ' active' : ''}`} onClick={tabFocus}>Expanded camera</button>
        <button type="button" aria-pressed={view === 'wall'} className={`tab${view === 'wall' ? ' active' : ''}`} onClick={tabWall}>Video wall</button>
      </div>

      <div className={`lv-wrap${view === 'wall' ? ' no-rail' : ''}`}>
        <aside className="lv-rail">
          <div className="inv-search" style={{ maxWidth: 'none', marginBottom: 'var(--s4)' }}>
            <span className="ico">⌕</span>
            <input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter cameras…" />
          </div>
          {/* Disabled cameras never reach the Live page, so there's no "Disabled" status to offer here. */}
          <CameraFilterBar cameras={enabled} value={cf} onChange={setCf} compact />
          <div className="label" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 'var(--s2)' }}>
            <span>Cameras</span><span>{filtered.length === enabled.length ? enabled.length : `${filtered.length} of ${enabled.length}`}</span>
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
                {pages > 1 && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    <button className="lv-ly" onClick={() => setPage(p => Math.max(0, p - 1))}
                            disabled={safePage === 0} aria-label="Previous cameras">‹</button>
                    {/* The range, not the page number. "10–18 of 103" answers
                        "where am I in the fleet"; "page 2 of 12" does not. */}
                    <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)',
                                   minWidth: 96, textAlign: 'center' }}
                          aria-live="polite">
                      {from + 1}–{from + shown.length} of {zoneCams.length}
                    </span>
                    <button className="lv-ly" onClick={() => setPage(p => Math.min(pages - 1, p + 1))}
                            disabled={safePage >= pages - 1} aria-label="Next cameras">›</button>
                  </div>
                )}
                <button className="btn-ghost btn-sm" onClick={() => pushWall()}>Push to wall</button>
              </div>
            </div>
          )}

          {view === 'wall' ? (
            <WallView cams={wallCams} prefs={wallPrefs} theme={theme} customised={Object.keys(local).length > 0}
                      title={branding.wall_title || DEFAULT_WALL_TITLE} logoUrl={logoUrl}
                      onRemove={wallRemove} onClear={wallClear}
                      onPick={() => setPickerOpen(true)} onSettings={() => setSettingsOpen(true)} />
          ) : focusCam ? (
            <>
              <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
                <button className="btn-primary btn-sm" onClick={tabGrid}>Show all</button>
              </div>
              <ExpandedView cam={focusCam} uptime={uptime[focusCam.id]} onPushWall={() => pushWall(focusCam.id)} />
            </>
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
            <div ref={gridRef} style={{ display: 'grid', gap: 12, gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
              {shown.map((c, i) => (
                <div key={c.id} className="lv-tile" title="Expand" onClick={() => setFocus(c.id)}>
                  {/* THE LOW-RES BUDGET LIVES HERE NOW. The wall is six main
                      streams; this grid can be sixteen, and a tile that small
                      cannot show the detail a main stream costs. Past nine
                      tiles it asks for the camera's sub track up front instead
                      of falling back to it only after the main fails; the
                      LOW-RES badge says so, and expanding is always the main. */}
                  <LiveVideo slug={c.slug} variant="tile" staggerMs={i * 250}
                              hasSub={!!c.sub_track?.url_raw} preferSub />
                  <LvBadges c={c} pos={ptzPos[c.id]} />
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
      <WallPicker open={pickerOpen} cameras={enabled} selected={wallPrefs.followAll ? [] : wallPrefs.cameraIds}
                  followAll={wallPrefs.followAll} isAdmin={isAdmin}
                  onClose={() => setPickerOpen(false)}
                  onApply={async (ids, all, forEveryone) => {
                    const what = all ? 'all cameras' : `${ids.length} camera${ids.length === 1 ? '' : 's'}`;
                    try {
                      if (forEveryone && isAdmin) {
                        await publishDefaults({ followAll: all, cameraIds: all ? [] : ids }, ['followAll', 'cameraIds']);
                        toast(`Default wall for everyone: ${what}`);
                      } else {
                        override({ followAll: all, cameraIds: all ? [] : ids });
                        toast(`This screen shows ${what}`);
                      }
                      setPickerOpen(false);
                    } catch (e: any) { toast(e?.message || 'Could not save the default', 'err'); }
                  }} />
      {/* Everyone gets ⚙ — the look of THIS screen is theirs to tune; saving it as everyone's
          default, and the logos/title, stay admin-only. */}
      <WallSettings open={settingsOpen} isAdmin={isAdmin} branding={branding} theme={theme}
                    logoLightUrl={logoLightUrl} logoDarkUrl={logoDarkUrl}
                    prefs={wallPrefs} defaults={defaults} local={local}
                    onOverride={override} onReset={resetLocal}
                    onPublish={() => publishDefaults(wallPrefs, Object.keys(local) as WallKey[])}
                    onClose={() => setSettingsOpen(false)} onChanged={refreshBranding} />
    </ViewTransition>
  );
}
