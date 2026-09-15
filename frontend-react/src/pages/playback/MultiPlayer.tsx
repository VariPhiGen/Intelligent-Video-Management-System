/**
 * MultiPlayer.tsx — synced multi-camera playback.
 *
 * Up to 4 tiles play the same instant, each from its own HLS VOD playlist over
 * the NVR's recorded segments — so a tile fetches only the minute it is about
 * to show, and the tiles stay aligned by wall clock (PROGRAM-DATE-TIME) rather
 * than by all having been cut from the same 2-minute build.
 *
 * This tab used to `Promise.all` a `/nvr/clip?before=0&after=120` blob per tile.
 * That was the 503 generator: 4 tiles meant 4 simultaneous clip builds against
 * a 4-slot semaphore, so a 5th request anywhere in the product was rejected.
 * Measured against the live NVR — 6 concurrent viewers of one HEVC camera:
 * `/clip` returned 4×200 at ~16 s and 2×503 "Clip extraction at capacity";
 * the HLS path returned 6×200 at 2.3 s, because the NVR collapses concurrent
 * requests for one segment into a single ffmpeg and serves the rest from cache.
 *
 * Navigation is the SAME rich timeline the single player uses — the very same
 * <Timeline> component, here in multi-lane mode (one coverage lane per camera,
 * one shared playhead). Pan, zoom (10 min–7 days) and click-to-seek all move
 * every tile together. There is no separate "Load" button: scrubbing the
 * timeline, jumping to a time, or hitting LIVE is what loads footage.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Hls from 'hls.js';
import { ApiError } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import { DateTimeField } from '@/components/DateTimeField';
import { CameraRail } from './CameraRail';
import { fmtWall, nvrCoverage, toSec, type NvrCamera } from './coverage';
import {
  RELOAD_MIN_INTERVAL_MS, createHls, datedFragments, detectHevc, epochAt,
  hlsResponseError, mediaTimeFor, playlistUrl, useBearerToken,
} from './hlsShared';
import { NoFootage, type TileState } from './NoFootage';
import { Timeline } from './Timeline';

const CHUNK_MS = 120_000;
/**
 * How far a tile may drift from the leading tile before it is nudged back.
 *
 * Above the ~0.5 s the clock tick itself can account for, below what an
 * operator comparing two views would notice.
 */
const SYNC_TOLERANCE_S = 1.0;
const SPEEDS = [1, 2, 4, 8, 16];
const ZOOMS = [1, 6, 24];             // hours
interface Range { earliest: number; latest: number }   // epoch seconds
interface Gap { start: number; end: number }           // epoch seconds

/**
 * Turn a failed footage request into a tile state. "No footage here" is the
 * expected outcome of scrubbing an NVR, not an error — only a real fault gets
 * the error treatment. The NVR's 404 carries `recorded_range`, which lets the
 * tile offer a jump; a gap inside the range carries none, so fall back to the
 * camera's extents.
 *
 * Unchanged by the move to HLS: the playlist 404 carries the identical body as
 * `/clip`'s did, and `hlsResponseError` rebuilds the same `ApiError` from what
 * hls.js reports, so this classification has exactly one implementation.
 */
function classifyClipError(e: unknown, fallback?: Range | null): TileState {
  if (!(e instanceof ApiError)) return { kind: 'error', message: (e as Error)?.message || 'Unknown error' };
  if (e.status !== 404) return { kind: 'error', message: e.message };
  const d = e.detail;
  const r = d?.recorded_range;
  if (r?.earliest && r?.latest) return { kind: 'gap', earliest: Date.parse(r.earliest), latest: Date.parse(r.latest) };
  if (typeof d?.error === 'string' && /no recordings|not found/i.test(d.error)) return { kind: 'none' };
  if (fallback) return { kind: 'gap', earliest: fallback.earliest * 1000, latest: fallback.latest * 1000 };
  return { kind: 'gap' };
}

export function MultiPlayer({ nvrCams, registry }: { nvrCams: NvrCamera[]; registry: Camera[] }) {
  const toast = useToast();

  const [sel, setSel] = useState<string[]>([]);
  const [camFilter, setCamFilter] = useState('');   // query for the add-camera picker
  const [addOpen, setAddOpen] = useState(false);     // add-camera dropdown open?
  const [view, setView] = useState(() => ({ start: Date.now() - 24 * 3600_000, end: Date.now() }));
  const [gaps, setGaps] = useState<Record<string, Gap[]>>({});
  const [playing, setPlaying] = useState(false);
  // Where the jump-to control is pointed. Kept so the box reflects the last
  // jump rather than clearing itself the moment you use it. (`jumpTo` further
  // down is a different thing — the recover-from-gap callback.)
  const [seekAt, setSeekAt] = useState('');
  const [rate, setRate] = useState(1);
  const [tileStatus, setTileStatus] = useState<Record<string, TileState | null>>({});
  const [clockMs, setClockMs] = useState<number | null>(null);

  const vids = useRef<Record<string, HTMLVideoElement | null>>({});
  // One hls.js instance and one fragment list per tile. The fragments are what
  // make the shared playhead work: a tile's wall clock is read from its own
  // PROGRAM-DATE-TIME tags, so tiles whose segments start at different instants
  // still agree on the time being shown.
  const hlses = useRef<Record<string, Hls | null>>({});
  const tileFrags = useRef<Record<string, ReturnType<typeof datedFragments>>>({});
  const endedCount = useRef(0);
  const loadToken = useRef(0);
  const lastReload = useRef(0);
  // Assigned below, once the clock it reads is in scope. Held as a ref because
  // loadAt's error handler needs it and is defined first.
  const reloadRef = useRef<() => void>(() => {});

  const hlsOk = useMemo(() => Hls.isSupported(), []);
  const hevc = useMemo(detectHevc, []);
  const { bearer, refresh: refreshToken } = useBearerToken(hlsOk);
  const selRef = useRef(sel); selRef.current = sel;
  const playingRef = useRef(playing); playingRef.current = playing;
  const rateRef = useRef(rate); rateRef.current = rate;
  const viewRef = useRef(view); viewRef.current = view;
  const clockRef = useRef<number | null>(null); clockRef.current = clockMs;
  const autoLoaded = useRef(false);

  const regName = useCallback(
    (n: string) => registry.find(x => x.slug === n)?.name || n, [registry]);

  // Each camera's recorded extent, straight off /nvr/cameras — no extra call.
  const ranges = useMemo(() => {
    const m: Record<string, Range | null> = {};
    for (const c of nvrCams) {
      m[c.name] = c.earliest && c.latest
        ? { earliest: Date.parse(c.earliest) / 1000, latest: Date.parse(c.latest) / 1000 }
        : null;
    }
    return m;
  }, [nvrCams]);
  const rangesRef = useRef(ranges); rangesRef.current = ranges;

  const videos = () =>
    selRef.current.map(n => vids.current[n]).filter((v): v is HTMLVideoElement => !!v);

  const stopTiles = useCallback((keepFlags = false) => {
    loadToken.current++;
    Object.entries(hlses.current).forEach(([n, h]) => {
      if (h) { try { h.destroy(); } catch { /* already gone */ } }
      hlses.current[n] = null;
    });
    tileFrags.current = {};
    Object.values(vids.current).forEach(v => {
      if (!v) return;
      try { v.pause(); v.removeAttribute('src'); v.load(); } catch { /* detached */ }
    });
    if (!keepFlags) { setPlaying(false); setClockMs(null); }
  }, []);

  useEffect(() => () => { stopTiles(); }, [stopTiles]);

  // ── point every tile at `epochSec` (epoch seconds) ──
  const loadAt = useCallback(async (epochSec: number) => {
    const cams = selRef.current;
    if (!cams.length) { toast('Pick at least one camera', 'err'); return; }
    if (!hlsOk) { toast('This browser cannot play recorded video', 'err'); return; }
    stopTiles(true);
    // Show the requested instant immediately; the playhead switches to the
    // tiles' own PDT tags as soon as their fragments land.
    setClockMs(epochSec * 1000);
    endedCount.current = 0;
    lastReload.current = Date.now();
    const token = loadToken.current;
    setTileStatus(Object.fromEntries(cams.map(n => [n, { kind: 'loading' } as TileState])));
    await refreshToken();
    if (token !== loadToken.current) return;
    // Set the ref alongside the state: playingRef only catches up on the next
    // render, and LEVEL_LOADED can fire before that — a tile reading a stale
    // `false` would load correctly and then just sit there. Same idiom as
    // changeSpeed below.
    setPlaying(true); playingRef.current = true;

    for (const n of cams) {
      const v = vids.current[n];
      if (!v) continue;
      // Four of these run at once, so each buffers less than the single player.
      const inst = createHls(bearer, { maxBufferLength: 30, backBufferLength: 30 });
      hlses.current[n] = inst;

      inst.on(Hls.Events.LEVEL_LOADED, (_e, data) => {
        if (token !== loadToken.current) return;
        const frags = datedFragments(data.details.fragments);
        tileFrags.current[n] = frags;
        if (!frags.length) {
          setTileStatus(st => ({ ...st, [n]: { kind: 'gap' } }));
          return;
        }
        const media = mediaTimeFor(frags, epochSec);
        if (media != null) v.currentTime = media;
        try { v.playbackRate = rateRef.current; } catch { /* not ready */ }
        if (playingRef.current) v.play().catch(() => {});
        // The tile stays in 'loading' until the element can actually show a
        // frame. This event is the *playlist* landing, which is an index query
        // — clearing here would drop the spinner and leave a black tile for the
        // whole real wait, which on a transcoded HEVC camera is seconds.
      });

      inst.on(Hls.Events.ERROR, (_e, data) => {
        if (token !== loadToken.current) return;
        // A 409 means grooming rewrote a segment under us — the playlist's
        // content signatures are stale, so rebuild rather than fail the tile.
        if (data.response?.code === 409) { reloadRef.current(); return; }
        if (data.response?.code === 401) { refreshToken().then(() => inst.startLoad()); return; }
        if (!data.fatal) return;
        try { inst.destroy(); } catch { /* already gone */ }
        if (hlses.current[n] === inst) hlses.current[n] = null;
        setTileStatus(st => ({
          ...st, [n]: classifyClipError(hlsResponseError(data), rangesRef.current[n]),
        }));
      });

      inst.loadSource(playlistUrl(n, epochSec, hevc));
      inst.attachMedia(v);
    }
  }, [bearer, hevc, hlsOk, refreshToken, stopTiles, toast]);
  const loadRef = useRef(loadAt); loadRef.current = loadAt;

  // Seek = load a window at that instant, and keep the timeline framing it.
  const doSeek = useCallback((epochSec: number) => {
    const v = viewRef.current;
    const span = v.end - v.start;
    const ms = epochSec * 1000;
    // Only re-frame if the target left the current window (keeps scrubbing calm).
    if (ms < v.start || ms > v.end) setView({ start: ms - span / 2, end: ms + span / 2 });
    loadRef.current(epochSec);
  }, []);
  const doSeekRef = useRef(doSeek); doSeekRef.current = doSeek;

  const newestLatest = useCallback(() => {
    const ls = selRef.current.map(n => rangesRef.current[n]?.latest).filter((x): x is number => x != null);
    return ls.length ? Math.max(...ls) : null;
  }, []);

  const goLive = useCallback(() => {
    const newest = newestLatest();
    if (newest == null) { toast('No recorded footage yet', 'err'); return; }
    const endMs = newest * 1000 + 10 * 60_000;
    setView({ start: endMs - 24 * 3600_000, end: endMs });
    loadRef.current(newest - 120);
  }, [newestLatest, toast]);

  // Seed with the first ≤4 recorded cameras, then auto-play near live (once).
  useEffect(() => {
    if (autoLoaded.current || !nvrCams.length) return;
    autoLoaded.current = true;
    setSel(nvrCams.slice(0, 4).map(c => c.name));
  }, [nvrCams]);

  // First real selection → land on the latest footage, playing.
  const started = useRef(false);
  useEffect(() => {
    if (started.current || !sel.length) return;
    const newest = newestLatest();
    if (newest == null) return;
    started.current = true;
    const endMs = newest * 1000 + 10 * 60_000;
    setView({ start: endMs - 24 * 3600_000, end: endMs });
    doSeekRef.current(newest - 120);
  }, [sel, newestLatest]);

  // ── coverage per selected camera, debounced on view/selection ──
  const covCtl = useRef<AbortController | null>(null);
  const covTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchCoverage = useCallback(async () => {
    const cams = selRef.current;
    const { start, end } = viewRef.current;
    if (!cams.length) { setGaps({}); return; }
    if (covCtl.current) covCtl.current.abort();
    const ctl = new AbortController(); covCtl.current = ctl;
    try {
      const entries = await Promise.all(cams.map(async n => {
        try {
          const d = await nvrCoverage(n, new Date(start), new Date(end), 30, ctl.signal);
          return [n, (Array.isArray(d.gaps) ? d.gaps : []).map(g => ({ start: toSec(g.start), end: toSec(g.end) }))] as const;
        } catch { return [n, [] as Gap[]] as const; }
      }));
      if (covCtl.current === ctl) setGaps(Object.fromEntries(entries));
    } finally { if (covCtl.current === ctl) covCtl.current = null; }
  }, []);
  useEffect(() => {
    if (covTimer.current) clearTimeout(covTimer.current);
    covTimer.current = setTimeout(fetchCoverage, 250);
    return () => { if (covTimer.current) clearTimeout(covTimer.current); };
  }, [view, sel, fetchCoverage]);

  // ── transport ──
  const skip = (sec: number) => { if (clockMs != null) doSeek(clockMs / 1000 + sec); };
  const playPause = () => {
    const next = !playing;
    setPlaying(next);
    videos().forEach(v => { next ? v.play().catch(() => {}) : v.pause(); });
  };
  const changeSpeed = (r: number) => {
    setRate(r); rateRef.current = r;
    videos().forEach(v => { try { v.playbackRate = r; } catch { /* not ready */ } });
  };
  const setZoom = (hours: number) => {
    // Anchor on the playhead and end the window there — "the last N hours up
    // to where I'm watching" — matching SinglePlayer, the LIVE button and the
    // default 24h view. Centring on the previous view's midpoint (what this
    // did before) meant clicking LIVE then 1h landed ~12h in the past: LIVE
    // sets a 24h window ending now, whose midpoint is now-12h.
    const span = hours * 3600_000;
    let end = clockMs ?? view.end;
    // Never show past the live edge — with the playhead a couple of minutes
    // behind the newest footage, a window centred on it would be half future.
    const newest = newestLatest();
    if (newest != null) end = Math.min(end, newest * 1000 + 10 * 60_000);
    setView({ start: end - span, end });
  };
  const toggleCam = (name: string) => {
    if (sel.includes(name)) { setSel(sel.filter(n => n !== name)); return; }
    if (sel.length >= 4) { toast('Up to 4 cameras — deselect one first', 'err'); return; }
    setSel([...sel, name]);
  };
  // A tile can decode now — this, not the playlist, is when its wait ends.
  const onTileReady = (name: string) =>
    setTileStatus(st => (st[name] && st[name]!.kind === 'loading' ? { ...st, [name]: null } : st));
  // Buffer ran dry (the next segment is still being built): show the spinner
  // again rather than a frozen frame.
  const onTileWaiting = (name: string) =>
    setTileStatus(st => (st[name] == null ? { ...st, [name]: { kind: 'loading' } } : st));

  const onTileEnded = () => {
    endedCount.current++;
    // Every tile has played out its playlist. With a 30-minute window that
    // means the recording ended, not that a 2-minute chunk did — so rebuild at
    // the playhead (picking up footage recorded since) rather than jumping
    // forward 120 s into what may be nothing.
    if (endedCount.current >= selRef.current.length && playingRef.current) {
      reloadRef.current();
    }
  };

  // A newly added tile should join at the current instant, not start blank —
  // and a deselected one must be torn down, or its player keeps fetching
  // segments for a tile nobody is looking at.
  const prevSel = useRef<string[]>([]);
  useEffect(() => {
    const added = sel.filter(n => !prevSel.current.includes(n));
    const removed = prevSel.current.filter(n => !sel.includes(n));
    prevSel.current = sel;
    for (const n of removed) {
      const h = hlses.current[n];
      if (h) { try { h.destroy(); } catch { /* already gone */ } }
      delete hlses.current[n];
      delete tileFrags.current[n];
    }
    if (started.current && added.length && clockMs != null) loadRef.current(clockMs / 1000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel]);

  // Playhead: read the wall clock off the first tile that has fragments.
  //
  // Not `loadStart + currentTime` any more. Each tile's playlist begins at the
  // segment boundary *containing* the requested instant, and those boundaries
  // differ per camera — so media time 0 is a different wall-clock moment on
  // each tile. The PROGRAM-DATE-TIME tags are what make one shared playhead
  // correct across all of them.
  useEffect(() => {
    const t = setInterval(() => {
      let ref: number | null = null;
      for (const n of selRef.current) {
        const v = vids.current[n];
        const frags = tileFrags.current[n];
        if (!v || !frags?.length) continue;
        const at = epochAt(frags, v.currentTime);
        if (at == null) continue;
        if (ref == null) { ref = at; setClockMs(at * 1000); continue; }
        // Resync drifted tiles. Tiles start playing as their own first fragment
        // lands, a few hundred ms apart, and that offset persists — the old
        // engine avoided it by cutting every tile from one build. "Sync lock ·
        // all at same time" is the promise this tab makes, so hold the tiles to
        // the leader rather than letting them wander.
        if (Math.abs(at - ref) > SYNC_TOLERANCE_S && v.readyState >= 2 && !v.seeking) {
          const media = mediaTimeFor(frags, ref);
          if (media != null) v.currentTime = media;
        }
      }
    }, 500);
    return () => clearInterval(t);
  }, []);

  /**
   * Rebuild every tile's playlist at the current playhead.
   *
   * Reached when all tiles run out of footage, and when grooming invalidates a
   * playlist. Rate-limited: at the live edge the playlists end a few minutes
   * short of the window asked for, so without a floor this would spin.
   */
  const reloadAtClock = useCallback(() => {
    if (Date.now() - lastReload.current < RELOAD_MIN_INTERVAL_MS) return;
    const at = clockRef.current;
    if (at != null) loadRef.current(at / 1000);
  }, []);
  reloadRef.current = reloadAtClock;

  const jumpTo = useCallback((range: { earliest: number; latest: number }) => {
    // range is ms here (from classifyClipError); land on the near edge.
    const nowMs = clockMs ?? viewRef.current.end;
    const target = nowMs < range.earliest ? range.earliest : Math.max(range.earliest, range.latest - CHUNK_MS);
    doSeek(target / 1000);
  }, [clockMs, doSeek]);

  const clockText = clockMs != null ? fmtWall(clockMs / 1000) : '—— ——:——:——';
  const cols = sel.length <= 1 ? '1fr' : '1fr 1fr';
  const lanes = sel.map(n => ({ label: regName(n), range: ranges[n] ?? null, gaps: gaps[n] ?? [] }));

  // Add-camera picker candidates: not already selected, matching the query
  // (display name or slug). Keeps the rail compact with 100+ cameras — only the
  // ≤4 selected show as chips; everything else lives behind this searchable list.
  return (
    <div className="fade" style={{ display: 'grid', gridTemplateColumns: '1fr 232px', gap: 14, alignItems: 'start' }}>
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'grid', gridTemplateColumns: cols, gap: 10 }}>
          {!sel.length && <div className="empty">Pick cameras from the panel on the right.</div>}
          {sel.map(n => (
            <div key={n} className="lv-tile" style={{ cursor: 'default', aspectRatio: '16/9' }}>
              <video ref={el => { vids.current[n] = el; }} muted playsInline onEnded={onTileEnded}
                     onCanPlay={() => onTileReady(n)} onPlaying={() => onTileReady(n)}
                     onWaiting={() => onTileWaiting(n)}
                     style={{ width: '100%', height: '100%', objectFit: 'contain', background: '#0a0e13' }} />
              {tileStatus[n] != null && <NoFootage state={tileStatus[n]!} onJump={jumpTo} onRetry={() => clockMs != null && loadAt(clockMs / 1000)} />}
              {tileStatus[n] == null && !vids.current[n]?.src && <NoFootage state={{ kind: 'idle' }} />}
              <div style={{ position: 'absolute', left: 9, bottom: 8, pointerEvents: 'none' }}>
                <div style={{ fontSize: 11.5, fontWeight: 500, color: '#fff', textShadow: '0 1px 3px rgba(0,0,0,.7)' }}>
                  {regName(n)}
                </div>
                <div style={{ fontFamily: 'var(--mono)', fontSize: 9, color: '#DCE3EC' }}>{n}</div>
              </div>
            </div>
          ))}
        </div>

        {/* Transport + shared multi-lane timeline (same component as single view) */}
        <div className="card" style={{ marginTop: 14, padding: 0, overflow: 'hidden' }}>
          <div className="pb-controls">
            <button className="btn-ghost btn-sm" title="Back 30s" onClick={() => skip(-30)}>−30s</button>
            <button className="btn-primary btn-sm" style={{ minWidth: 44 }} title="Play / Pause"
                    onClick={playPause}>{playing ? '⏸' : '▶'}</button>
            <button className="btn-ghost btn-sm" title="Forward 30s" onClick={() => skip(30)}>+30s</button>
            <div className="pb-group">
              {SPEEDS.map(s => (
                <button key={s} className={`btn-ghost btn-sm${rate === s ? ' active' : ''}`}
                        onClick={() => changeSpeed(s)}>{s}x</button>
              ))}
            </div>
            <button className="btn-ghost btn-sm" title="Jump to latest footage" onClick={goLive}>LIVE</button>
            <span className="pb-ts" style={{ flex: 1, textAlign: 'center' }}>{clockText}</span>
            <div className="pb-group">
              {ZOOMS.map(h => {
                const active = Math.abs((view.end - view.start) / 3600_000 - h) < 0.5;
                return (
                  <button key={h} className={`btn-ghost btn-sm${active ? ' active' : ''}`}
                          onClick={() => setZoom(h)}>{h}h</button>
                );
              })}
            </div>
            {/* Split date + time: as a single datetime-local this was a jump
                control you could only aim at a day, because Chromium's picker
                has no clock. Held in state now so the box shows where you
                jumped instead of resetting itself. Each half seeks on change —
                picking the day lands on its midnight, the time refines it. */}
            <DateTimeField label="Jump to" value={seekAt} style={{ width: 230 }}
              inputStyle={{ fontSize: 12 }}
              onChange={v => {
                setSeekAt(v);
                if (v) doSeek(new Date(v).getTime() / 1000);
              }} />
          </div>

          {sel.length > 0 && (
            <Timeline
              viewStart={view.start} viewEnd={view.end}
              range={null} gaps={[]} lanes={lanes}
              playhead={clockMs != null ? clockMs / 1000 : null}
              onSeek={sec => doSeek(sec)}
              onViewChange={(start, end) => setView({ start, end })}
            />
          )}
          <div className="d-hint" style={{ margin: '4px 16px 14px' }}>
            One lane per camera — filled where footage exists on the NVR. Click or scrub the timeline to move all
            tiles together; scroll to zoom. Sync lock keeps every tile on the same clock.
          </div>
        </div>
      </div>

      <CameraRail {...{ sel, nvrCams, regName, toggleCam, camFilter, setCamFilter, addOpen, setAddOpen }} />
    </div>
  );
}
