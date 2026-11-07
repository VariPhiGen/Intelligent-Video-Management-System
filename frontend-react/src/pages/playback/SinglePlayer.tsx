/**
 * SinglePlayer.tsx — single-camera timeline playback, ported from the legacy
 * pb-view (pbOpen/pbSelectCamera/pbRefreshRange/pbFetchCoverage/pbBind).
 * Video plays sequential 2-min chunks via useChunkPlayer; the timeline canvas
 * shows footage (green), gaps (red) and the playhead over a pan/zoomable
 * window; the header dropdown (display names) switches cameras. With `ai_analytics`, and on a
 * camera AI Config gives activities or person/vehicle indexing, a strip under
 * the footage bar marks when a person / vehicle was in view (for the kinds it
 * indexes) and its AI events (timelineMarks.ts): click one or step through
 * them with ◀ ▶ to seek to its pre-roll.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import type { Camera, NvrCoverage } from '@/lib/types';
import { useToast } from '@/components/Toast';
import type { PersonHit, SearchResponse } from '@/lib/smartsearch';
import { AnnotateLayer } from './AnnotateLayer';
import { useVideoZoom } from './useVideoZoom';
import { listLabelClasses, type LabelClass } from '@/lib/labelClasses';
import { CHUNK_SECONDS, covSpans, fmtWall, nvrCoverage, toSec, type NvrCamera } from './coverage';
import { SimilarModal } from './SimilarModal';
import { Timeline } from './Timeline';
import {
  adjacentMark, cameraHasAi, clipToFootage, fetchAdjacent, indexedKinds, PRESENCE_META,
  toMarkFromAdjacent, useTimelineMarks, type TimelineMark,
} from './timelineMarks';
import { useChunkPlayer } from './useChunkPlayer';
import { useHlsPlayer } from './useHlsPlayer';

interface Range { earliest: number; latest: number }        // epoch seconds
interface Gap { start: number; end: number }                // epoch seconds

const ZOOMS = [1, 6, 24];
const SPEEDS = [1, 2, 4];
const MARKS_PREF = 'vms-playback-marks';

export function SinglePlayer({ camera, nvrCams, registry, onSelectCamera, onOpenAt, onClose, startAt }: {
  camera: string;
  nvrCams: NvrCamera[];
  registry: Camera[];
  onSelectCamera: (name: string) => void;
  /** Open (camera, epoch seconds) — same-or-other camera. PlaybackPage keys
   *  this component on (camera, startAt), so the jump is a remount and the
   *  existing consume-once startAt path does the seeking. Used by the
   *  find-similar results. */
  onOpenAt?: (name: string, epochSec: number) => void;
  onClose: () => void;
  /** Epoch seconds to open at, instead of the newest footage. Set when another
   *  page deep-links to a moment (Smart Search hands off this way). Clamped to
   *  the camera's recorded range so an out-of-range link lands on real footage
   *  rather than a dead player. */
  startAt?: number | null;
}) {
  const toast = useToast();
  const videoRef = useRef<HTMLVideoElement>(null);

  const [range, setRange] = useState<Range | null>(null);
  const [gaps, setGaps] = useState<Gap[]>([]);
  // The coverage answer itself, with the window it answers for: what the
  // markers are cut to, so none is drawn over time with no footage.
  const [cov, setCov] = useState<{ data: NvrCoverage; from: number; to: number } | null>(null);
  const [view, setView] = useState(() => ({ start: Date.now() - 24 * 3600_000, end: Date.now() }));
  const [speed, setSpeed] = useState(1);
  const [jump, setJump] = useState('');
  // Annotate mode: a canvas overlay swallows pointer events (so a click draws
  // instead of toggling play) and the video is paused on entry — a box belongs
  // to ONE frame, and drawing on moving footage commits it to a frame the
  // operator never inspected.
  const [annotating, setAnnotating] = useState(false);
  // The taxonomy is admin-owned and small, so it is fetched once per mount
  // rather than per annotate-mode toggle. A failure leaves it empty, which the
  // layer renders as "no label classes" — the honest reading either way, since
  // a save without a class would be refused.
  const [labelClasses, setLabelClasses] = useState<LabelClass[]>([]);

  useEffect(() => {
    let alive = true;
    listLabelClasses()
      .then(all => { if (alive) setLabelClasses(all.filter(c => c.active)); })
      .catch(() => { if (alive) setLabelClasses([]); });
    return () => { alive = false; };
  }, []);
  // Search-by-example results for the last drawn box (null = modal closed).
  const [similar, setSimilar] = useState<SearchResponse<PersonHit> | null>(null);

  // ── AI event marks: the API enforces `ai_analytics`; the check here only
  //    spares a user without it a request that is certain to be refused. ──
  const { me } = useAuth();
  // Per camera: no marker UI at all unless AI Config has it doing something.
  const regCam = registry.find(c => c.slug === camera);
  const cameraAi = cameraHasAi(regCam);
  const kinds = useMemo(() => indexedKinds(regCam), [regCam]);
  const canEvents = !!me?.permissions?.ai_analytics && cameraAi;
  const [showMarks, setShowMarks] = useState(() => {
    try { return localStorage.getItem(MARKS_PREF) !== 'off'; } catch { return true; }
  });
  const toggleMarks = () => setShowMarks(v => {
    try { localStorage.setItem(MARKS_PREF, v ? 'off' : 'on'); } catch { /* private mode */ }
    return !v;
  });
  // Activities hidden from the strip — per session, since the set of
  // activities differs from camera to camera.
  const [hiddenActs, setHiddenActs] = useState<ReadonlySet<string>>(() => new Set());
  const marksOn = canEvents && showMarks;
  const { marks: rawMarks, truncated, status: marksStatus } = useTimelineMarks(
    camera, view, marksOn, kinds, typeof window !== 'undefined' ? window.innerWidth : 1200);
  // Only what can be played: a marker over a recording gap, or ahead of the
  // footage the NVR has indexed, is not drawn, counted or walked to.
  const marks = useMemo(
    () => (cov ? clipToFootage(rawMarks, covSpans(cov.data, cov.from, cov.to)) : []),
    [rawMarks, cov]);
  const shownMarks = useMemo(
    () => (hiddenActs.size ? marks.filter(m => !hiddenActs.has(m.activity)) : marks),
    [marks, hiddenActs]);
  const legend = useMemo(() => {
    // Indexed presence kinds first and always — "indexed and quiet" reads as
    // a 0, not as a row with no name — then the activities that fired.
    const presence = new Map<string, { label: string; color: string; n: number }>(
      kinds.map(k => [PRESENCE_META[k].key, { label: PRESENCE_META[k].label,
                                               color: PRESENCE_META[k].color, n: 0 }]));
    const events = new Map<string, { label: string; color: string; n: number }>();
    for (const m of marks) {
      const into = m.row === 'event' ? events : presence;
      const e = into.get(m.activity);
      if (e) e.n++; else into.set(m.activity, { label: m.label, color: m.color, n: 1 });
    }
    return [...presence.entries(),
            ...[...events.entries()].sort((a, b) => a[1].label.localeCompare(b[1].label))];
  }, [marks, kinds]);
  const toggleAct = (key: string) => setHiddenActs(prev => {
    const next = new Set(prev);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  // Consume-once: a deep-linked timestamp applies to the camera it arrived with.
  // Without this, switching cameras inside Playback would keep yanking the new
  // camera back to the original hit's time.
  const startAtRef = useRef<number | null>(startAt ?? null);

  // Refs for callbacks that must see the latest values without re-binding.
  const cameraRef = useRef(camera);
  const rangeRef = useRef(range);
  const gapsRef = useRef(gaps);
  const viewRef = useRef(view);
  rangeRef.current = range;
  gapsRef.current = gaps;
  viewRef.current = view;

  const viewHours = (view.end - view.start) / 3600_000;

  /** True when epoch sits in a known gap, clamped to the indexed frontier. */
  const inGap = useCallback((epoch: number, slop = 0): boolean => {
    const cap = rangeRef.current ? rangeRef.current.latest : Infinity;
    for (const g of gapsRef.current) {
      if (g.start >= cap) continue;
      const end = Math.min(g.end, cap);
      if (epoch >= g.start - slop && epoch < end + slop) return true;
    }
    return false;
  }, []);

  const playerHandlers = {
    inGap,
    // Server says this window is fully covered — overlapping gaps in our list
    // are stale (e.g. LIVE right after recording resumed). Drop them.
    onFullCoverage: (epoch: number) => {
      setGaps(gs => {
        const next = gs.filter(g => g.end <= epoch || g.start >= epoch + CHUNK_SECONDS);
        return next.length !== gs.length ? next : gs;
      });
    },
  };

  // Two engines, one <video>. HLS is the default: it plays the recorded
  // segments directly, so a seek costs an HTTP GET instead of an ffmpeg run.
  // The clip engine stays for browsers without MSE (iOS Safari — its native
  // HLS player can't attach our bearer token) and behind an escape hatch, so
  // an operator hitting an HLS-specific problem has somewhere to stand.
  const forceClips = useMemo(() => {
    try { return localStorage.getItem('vms-playback-engine') === 'clip'; }
    catch { return false; }
  }, []);
  // Digital zoom over the recorded frame. Single playback only — see
  // useVideoZoom for why the multi-view does not get it.
  const zoom = useVideoZoom();

  const hlsPlayer = useHlsPlayer(videoRef, playerHandlers, { enabled: !forceClips });
  const useHls = hlsPlayer.available && !forceClips;
  const clipPlayer = useChunkPlayer(videoRef, playerHandlers, { enabled: !useHls });
  const player = useHls ? hlsPlayer : clipPlayer;

  // ── coverage (gaps) — debounced, keyed, aborted like the legacy engine ──
  const covKey = useRef('');
  const covCtl = useRef<AbortController | null>(null);
  const covTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchCoverage = useCallback(async () => {
    const cam = cameraRef.current;
    const { start: vs, end: ve } = viewRef.current;
    // The view plus half a view either side — the window the markers are
    // fetched for, so every marker held has coverage to be cut against.
    const start = vs - (ve - vs) / 2, end = ve + (ve - vs) / 2;
    const span = Math.max(60_000, Math.round((ve - vs) / 16));
    const key = `${cam}|${Math.floor(start / span)}|${Math.ceil(end / span)}`;
    if (key === covKey.current) return;
    if (covCtl.current) covCtl.current.abort();
    const ctl = new AbortController();
    covCtl.current = ctl;
    try {
      const data = await nvrCoverage(cam, new Date(start), new Date(end), 30, ctl.signal);
      if (cam !== cameraRef.current) return;
      setGaps((Array.isArray(data.gaps) ? data.gaps : [])
        .map(g => ({ start: toSec(g.start), end: toSec(g.end) })));
      setCov({ data, from: start, to: end });
      covKey.current = key;
    } catch { /* aborted or NVR briefly away — next pan/zoom retries */ }
    finally { if (covCtl.current === ctl) covCtl.current = null; }
  }, []);

  const scheduleCoverage = useCallback(() => {
    if (covTimer.current) clearTimeout(covTimer.current);
    covTimer.current = setTimeout(fetchCoverage, 250);
  }, [fetchCoverage]);

  useEffect(() => { scheduleCoverage(); }, [view, camera, scheduleCoverage]);
  useEffect(() => () => {
    if (covTimer.current) clearTimeout(covTimer.current);
    if (covCtl.current) covCtl.current.abort();
  }, []);

  const refreshRange = useCallback(async (): Promise<Range | null> => {
    try {
      const r = await apiFetch<{ earliest: string; latest: string }>(
        `/nvr/cameras/${encodeURIComponent(camera)}/range`);
      if (cameraRef.current !== camera) return null;
      const rng = { earliest: Date.parse(r.earliest) / 1000, latest: Date.parse(r.latest) / 1000 };
      setRange(rng);
      // Gaps go stale as the index advances (the trailing "gap" closes as new
      // segments are indexed) — refetch alongside every range refresh.
      covKey.current = '';
      scheduleCoverage();
      return rng;
    } catch { return null; }
  }, [camera, scheduleCoverage]);

  // ── camera open / switch: reset, load range, start near-live ──
  useEffect(() => {
    cameraRef.current = camera;
    player.stop();
    setRange(null);
    setGaps([]);
    setCov(null);
    covKey.current = '';
    player.setOverlay('Loading footage range…');
    let cancelled = false;
    (async () => {
      const rng = await refreshRange();
      if (cancelled) return;
      if (!rng) { player.setOverlay('No footage recorded yet for this camera'); return; }
      if (startAtRef.current != null) {
        // Deep-linked to a moment: centre the timeline on it and seek there.
        const at = Math.min(Math.max(startAtRef.current, rng.earliest), rng.latest);
        startAtRef.current = null;
        setView({ start: at * 1000 - 12 * 3600_000, end: at * 1000 + 12 * 3600_000 });
        player.seek(camera, at);
        return;
      }
      // Default view: last 24h ending a bit past the newest footage; start "live".
      const end = rng.latest * 1000 + 10 * 60_000;
      setView({ start: end - 24 * 3600_000, end });
      player.seek(camera, Math.max(rng.earliest, rng.latest - CHUNK_SECONDS));
    })();
    const t = setInterval(refreshRange, 60000);
    return () => { cancelled = true; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera]);

  // ── controls ──
  const doSeek = useCallback((epoch: number, slop = 0) => {
    if (inGap(epoch, slop)) { player.showGapStop(); return; }
    player.seek(cameraRef.current, epoch);
  }, [inGap, player]);

  /** Seek to an event's pre-roll, panning the view if it is off screen. */
  const seekMark = (m: TimelineMark) => {
    const { start, end } = viewRef.current;
    const at = m.seekAt * 1000;
    if (at < start || m.start * 1000 > end) {
      const half = (end - start) / 2;
      setView({ start: m.start * 1000 - half, end: m.start * 1000 + half });
    }
    doSeek(m.seekAt);
  };
  /** ◀ ▶: the nearest loaded mark if there is one, otherwise ask the API —
   *  the next marker may be days away, far outside the loaded window. The
   *  same filters apply: hidden rows and activities are skipped either way. */
  const stepping = useRef(false);
  const stepMark = async (dir: 1 | -1) => {
    const at = player.currentEpoch();
    const local = adjacentMark(shownMarks, at, dir);
    if (local) { seekMark(local); return; }
    if (at == null || stepping.current) return;
    stepping.current = true;
    try {
      const shownKinds = kinds.filter(k => !hiddenActs.has(PRESENCE_META[k].key));
      const hiddenEvents = [...hiddenActs].filter(k => !k.startsWith('presence:'));
      // The API knows markers, not footage: an answer inside a recording gap
      // is skipped and the search goes on past it (bounded — a camera that
      // records nothing for days is not worth a hundred round trips).
      let from = at;
      for (let hop = 0; hop < 12; hop++) {
        const m = await fetchAdjacent(camera, from, dir, shownKinds, true, hiddenEvents);
        if (cameraRef.current !== camera) return;
        if (!m) break;
        const mark = toMarkFromAdjacent(m);
        const end = m.end ?? m.start;
        const around = await nvrCoverage(camera, new Date((m.seek_at - 60) * 1000),
                                          new Date((end + 60) * 1000), 30);
        if (cameraRef.current !== camera) return;
        const playable = clipToFootage([mark], covSpans(around, (m.seek_at - 60) * 1000,
                                                         (end + 60) * 1000));
        if (playable.length) {
          seekMark(dir === 1 ? playable[0] : playable[playable.length - 1]);
          return;
        }
        from = m.seek_at;
      }
      toast(dir === 1 ? 'Nothing later with footage on this camera'
                      : 'Nothing earlier with footage on this camera');
    } catch {
      toast('Could not look up the next marker', 'err');
    } finally {
      stepping.current = false;
    }
  };

  const skip = (sec: number) => {
    const cur = player.currentEpoch();
    if (cur != null) doSeek(cur + sec);
  };
  const goLive = () => {
    const rng = rangeRef.current;
    if (rng) doSeek(rng.latest - CHUNK_SECONDS);
  };
  const setZoom = (hours: number) => {
    // Anchor on where the user is actually watching ("that time"), not the
    // geometric middle of the previous view. Clicking 6h/1h now shows the N
    // hours up to the playhead — the same "last N hours" model as the default
    // 24h view and the LIVE button — instead of a window centred on the old
    // view's midpoint (which drifts far from the current frame).
    const cur = player.currentEpoch();
    const end = cur != null ? cur * 1000 : view.end;
    setView({ start: end - hours * 3600_000, end });
  };
  const changeSpeed = (s: number) => { setSpeed(s); player.setRate(s); };

  const onJump = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter') return;
    const val = jump.trim();
    if (!val) return;
    let d = new Date(val);
    if (isNaN(d.getTime())) d = new Date(val.replace(' ', 'T'));
    if (isNaN(d.getTime())) { toast('Use YYYY-MM-DD HH:MM:SS', 'err'); return; }
    doSeek(d.getTime() / 1000);
    const end = d.getTime() + viewHours * 1800_000;
    setView({ start: end - viewHours * 3600_000, end });
  };

  const onDatePick = (val: string) => {
    if (!val) return;
    const [y, m, d] = val.split('-').map(Number);
    const dayStart = new Date(y, m - 1, d).getTime();
    setView({ start: dayStart, end: dayStart + viewHours * 3600_000 });
  };

  // The dropdown shows what operators call the cameras, sorted that way. The
  // value stays the slug (the NVR's recording name); two cameras sharing a
  // display name get their slug appended so they can still be told apart.
  const cameraOptions = useMemo(() => {
    const list = (nvrCams.length ? nvrCams : [{ name: camera } as NvrCamera])
      .map(c => ({ value: c.name, label: registry.find(r => r.slug === c.name)?.name || c.name }));
    const seen = new Map<string, number>();
    for (const o of list) seen.set(o.label, (seen.get(o.label) ?? 0) + 1);
    return list
      .map(o => (seen.get(o.label)! > 1 && o.label !== o.value ? { ...o, label: `${o.label} (${o.value})` } : o))
      .sort((a, b) => a.label.localeCompare(b.label, undefined, { numeric: true, sensitivity: 'base' }));
  }, [nvrCams, registry, camera]);

  // Fullscreen the whole player card, not just the <video>: the timeline, markers and the annotate bar stay usable
  // (a bare video element in fullscreen would drop all of them). `F` toggles, unless typing in a field; Esc is the
  // browser's own exit.
  const cardRef = useRef<HTMLDivElement>(null);
  const [fs, setFs] = useState(false);
  const toggleFs = useCallback(() => {
    if (document.fullscreenElement) void document.exitFullscreen?.();
    else void cardRef.current?.requestFullscreen?.();
  }, []);
  useEffect(() => {
    const onChange = () => setFs(document.fullscreenElement === cardRef.current);
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key !== 'f' && e.key !== 'F') return;
      if (e.ctrlKey || e.metaKey || e.altKey || (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)))) return;
      e.preventDefault();
      toggleFs();
    };
    document.addEventListener('fullscreenchange', onChange);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('fullscreenchange', onChange); document.removeEventListener('keydown', onKey); };
  }, [toggleFs]);
  // "Find similar" opens a modal OUTSIDE the card, which fullscreen would hide — leave fullscreen so it's seen.
  useEffect(() => { if (similar && document.fullscreenElement) void document.exitFullscreen?.(); }, [similar]);

  return (
    <div id="pb-view" className="fade">
      {similar && onOpenAt && (
        <SimilarModal outcome={similar} onClose={() => setSimilar(null)}
                      onOpenAt={(slug, epoch) => { setSimilar(null); onOpenAt(slug, epoch); }} />
      )}
      <div ref={cardRef} className={`card pb-card${fs ? ' pb-fs' : ''}`} style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px',
                      borderBottom: '1px solid var(--border)', flexWrap: 'wrap' }}>
          <button className="btn-ghost btn-sm" onClick={onClose}>← Cameras</button>
          <select value={camera} onChange={e => onSelectCamera(e.target.value)}
                  title={camera} style={{ maxWidth: 320, width: 'auto' }}>
            {cameraOptions.map(o => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <div style={{ flex: 1 }} />
          <span className="pb-ts">{player.wall != null ? fmtWall(player.wall) : '--:--:--'}</span>
          <input value={jump} onChange={e => setJump(e.target.value)} onKeyDown={onJump}
                 type="text" placeholder="YYYY-MM-DD HH:MM:SS"
                 title="Jump to a date/time (Enter)" style={{ maxWidth: 190, fontSize: 12 }} />
          <button className="btn-ghost btn-sm" onClick={toggleFs}
                  title={fs ? 'Exit fullscreen (F or Esc)' : 'Fullscreen — video, timeline and annotate (F)'}
                  aria-label={fs ? 'Exit fullscreen' : 'Fullscreen'}>
            {fs ? '⤡ Exit fullscreen' : '⤢ Fullscreen'}
          </button>
        </div>

        <div className="pb-video-wrap">
          {/* The stage carries the zoom transform and holds BOTH the video and
              (portalled in) the annotate canvas, so a box drawn while zoomed
              maps to the same source pixels as one drawn at 1x. Overlays and
              the annotate toolbar stay outside it — chrome must not scale. */}
          <div className="pb-stage" ref={zoom.attachStage} style={zoom.style}
               onPointerDown={zoom.onPanStart}
               onPointerMove={zoom.onPanMove}
               onPointerUp={zoom.onPanEnd}
               onPointerCancel={zoom.onPanEnd}
               onContextMenu={e => { if (zoom.zoomed) e.preventDefault(); }}>
            <video ref={videoRef} playsInline onClick={player.togglePlay} />
          </div>
          {player.overlay != null && <div className="pb-overlay"><div>{player.overlay}</div></div>}
          {player.loading && <div className="pb-overlay"><span className="spinner" /></div>}
          {player.gapWarn && <div className="pb-gapwarn">Recording gap detected in this window</div>}

          {/* Zoom controls. Hidden while the frame is at 1x would be tidier and
              worse: an operator cannot discover a control that is not there. */}
          <div className="pb-zoom" role="group" aria-label="Zoom">
            <button className="btn-ghost btn-sm iconish" onClick={zoom.zoomOut}
                    disabled={!zoom.zoomed} aria-label="Zoom out" title="Zoom out">−</button>
            <span className="pb-zoom-level" aria-live="polite">
              {zoom.tf.scale.toFixed(1)}×
            </span>
            <button className="btn-ghost btn-sm iconish" onClick={zoom.zoomIn}
                    aria-label="Zoom in" title="Zoom in (scroll on the video)">+</button>
            <button className="btn-ghost btn-sm" onClick={zoom.reset}
                    disabled={!zoom.zoomed} title="Reset zoom">Reset</button>
          </div>
          {zoom.zoomed && (
            <div className="pb-zoom-hint">Right-drag to pan</div>
          )}

          {/* key: a camera switch discards any half-drawn box — the capture
              belongs to the previous camera's frame. */}
          {annotating && (
            <AnnotateLayer key={camera} videoRef={videoRef} camera={camera}
                           classes={labelClasses}
                           epochOf={player.currentEpoch}
                           stage={zoom.stageEl}
                           onSimilar={onOpenAt ? setSimilar : undefined} />
          )}
        </div>

        <div className="pb-controls">
          <button className="btn-ghost btn-sm" title="Back 30s" onClick={() => skip(-30)}>−30s</button>
          <button className="btn-primary btn-sm" style={{ minWidth: 44 }} title="Play / Pause"
                  onClick={player.togglePlay}>{player.paused ? '▶' : '⏸'}</button>
          <button className="btn-ghost btn-sm" title="Forward 30s" onClick={() => skip(30)}>+30s</button>
          <div className="pb-group">
            {SPEEDS.map(s => (
              <button key={s} className={`btn-ghost btn-sm${speed === s ? ' active' : ''}`}
                      onClick={() => changeSpeed(s)}>{s}x</button>
            ))}
          </div>
          <button className="btn-ghost btn-sm" title="Jump to latest footage" onClick={goLive}>LIVE</button>
          <button
            className={`btn-ghost btn-sm${annotating ? ' active' : ''}`}
            title="Draw a box on the current frame and save it as an annotation"
            onClick={() => {
              setAnnotating(a => {
                const next = !a;
                if (next) videoRef.current?.pause(); // a box belongs to one frame
                return next;
              });
            }}>
            ✏ Annotate
          </button>
          {canEvents && (
            <div className="pb-group">
              <button className={`btn-ghost btn-sm${showMarks ? ' active' : ''}`}
                      aria-pressed={showMarks} onClick={toggleMarks}
                      title="Show AI markers on the timeline">◆ Markers</button>
              {showMarks && (
                <>
                  <button className="btn-ghost btn-sm" title="Previous marker"
                          onClick={() => stepMark(-1)}>◀</button>
                  <button className="btn-ghost btn-sm" title="Next marker"
                          onClick={() => stepMark(1)}>▶</button>
                </>
              )}
            </div>
          )}
          <div style={{ flex: 1 }} />
          <div className="pb-group">
            {ZOOMS.map(h => (
              <button key={h} className={`btn-ghost btn-sm${Math.abs(viewHours - h) < 0.5 ? ' active' : ''}`}
                      onClick={() => setZoom(h)}>{h}h</button>
            ))}
          </div>
          <input type="date" style={{ width: 150, fontSize: 12 }}
                 onChange={e => onDatePick(e.target.value)} />
        </div>

        {marksOn && (
          <div className="pb-marks-bar" aria-live="polite">
            {marksStatus === 'unavailable' && <span>Events are not available for this camera.</span>}
            {marksStatus !== 'unavailable' && !legend.length && (
              <span>{marksStatus === 'loading' ? 'Loading markers…' : 'No AI events in this window.'}</span>
            )}
            {legend.map(([key, a]) => (
              <button key={key} className="pb-mark-chip" aria-pressed={!hiddenActs.has(key)}
                      title={hiddenActs.has(key) ? `Show ${a.label}` : `Hide ${a.label}`}
                      onClick={() => toggleAct(key)}>
                <span className="pb-mark-dot" style={{ background: a.color }} />
                {a.label} <span style={{ color: 'var(--muted)' }}>{a.n}</span>
              </button>
            ))}
            {truncated && <span title="Zoom in to see every event">· newest shown only — zoom in for all</span>}
          </div>
        )}
        <Timeline
          viewStart={view.start} viewEnd={view.end}
          range={range} gaps={gaps} playhead={player.wall}
          marks={marksOn ? shownMarks : undefined}
          presenceRows={marksOn ? kinds.filter(k => !hiddenActs.has(PRESENCE_META[k].key)) : undefined}
          onMarkClick={seekMark}
          onSeek={doSeek}
          onViewChange={(start, end) => setView({ start, end })}
        />
      </div>
    </div>
  );
}
