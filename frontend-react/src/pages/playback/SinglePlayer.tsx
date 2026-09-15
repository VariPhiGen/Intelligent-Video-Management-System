/**
 * SinglePlayer.tsx — single-camera timeline playback, ported from the legacy
 * pb-view (pbOpen/pbSelectCamera/pbRefreshRange/pbFetchCoverage/pbBind).
 * Video plays sequential 2-min chunks via useChunkPlayer; the timeline canvas
 * shows footage (green), gaps (red) and the playhead over a pan/zoomable
 * window; the right rail switches cameras.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import type { PersonHit, SearchResponse } from '@/lib/smartsearch';
import { AnnotateLayer } from './AnnotateLayer';
import { CHUNK_SECONDS, fmtWall, nvrCoverage, toSec, type NvrCamera } from './coverage';
import { SimilarModal } from './SimilarModal';
import { Timeline } from './Timeline';
import { useChunkPlayer } from './useChunkPlayer';
import { useHlsPlayer } from './useHlsPlayer';

interface Range { earliest: number; latest: number }        // epoch seconds
interface Gap { start: number; end: number }                // epoch seconds

const ZOOMS = [1, 6, 24];
const SPEEDS = [1, 2, 4];

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
  const [view, setView] = useState(() => ({ start: Date.now() - 24 * 3600_000, end: Date.now() }));
  const [speed, setSpeed] = useState(1);
  const [jump, setJump] = useState('');
  // Annotate mode: a canvas overlay swallows pointer events (so a click draws
  // instead of toggling play) and the video is paused on entry — a box belongs
  // to ONE frame, and drawing on moving footage commits it to a frame the
  // operator never inspected.
  const [annotating, setAnnotating] = useState(false);
  // Search-by-example results for the last drawn box (null = modal closed).
  const [similar, setSimilar] = useState<SearchResponse<PersonHit> | null>(null);

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
    const { start, end } = viewRef.current;
    const span = Math.max(60_000, Math.round((end - start) / 16));
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

  const regName = (name: string) => registry.find(c => c.slug === name)?.name || name;

  return (
    <div id="pb-view" className="fade">
      {similar && onOpenAt && (
        <SimilarModal outcome={similar} onClose={() => setSimilar(null)}
                      onOpenAt={(slug, epoch) => { setSimilar(null); onOpenAt(slug, epoch); }} />
      )}
      <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '12px 16px',
                      borderBottom: '1px solid var(--border)', flexWrap: 'wrap' }}>
          <button className="btn-ghost btn-sm" onClick={onClose}>← Cameras</button>
          <select value={camera} onChange={e => onSelectCamera(e.target.value)}
                  style={{ maxWidth: 320, width: 'auto' }}>
            {(nvrCams.length ? nvrCams : [{ name: camera } as NvrCamera]).map(c => (
              <option key={c.name} value={c.name}>{c.name}</option>
            ))}
          </select>
          <div style={{ flex: 1 }} />
          <span className="pb-ts">{player.wall != null ? fmtWall(player.wall) : '--:--:--'}</span>
          <input value={jump} onChange={e => setJump(e.target.value)} onKeyDown={onJump}
                 type="text" placeholder="YYYY-MM-DD HH:MM:SS"
                 title="Jump to a date/time (Enter)" style={{ maxWidth: 190, fontSize: 12 }} />
        </div>

        <div className="pb-video-wrap">
          <video ref={videoRef} playsInline onClick={player.togglePlay} />
          {player.overlay != null && <div className="pb-overlay"><div>{player.overlay}</div></div>}
          {player.loading && <div className="pb-overlay"><span className="spinner" /></div>}
          {player.gapWarn && <div className="pb-gapwarn">Recording gap detected in this window</div>}
          {/* key: a camera switch discards any half-drawn box — the capture
              belongs to the previous camera's frame. */}
          {annotating && (
            <AnnotateLayer key={camera} videoRef={videoRef} camera={camera}
                           epochOf={player.currentEpoch}
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

        <Timeline
          viewStart={view.start} viewEnd={view.end}
          range={range} gaps={gaps} playhead={player.wall}
          onSeek={doSeek}
          onViewChange={(start, end) => setView({ start, end })}
        />
      </div>

      {/* Cameras rail (legacy pbRailRender) */}
      <div className="card" style={{ padding: 14 }}>
        <div style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: '.12em', textTransform: 'uppercase',
                      color: 'var(--dim)', marginBottom: 10 }}>Cameras</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 1, fontSize: 12.5 }}>
          {!nvrCams.length && <p style={{ color: 'var(--dim)', fontSize: 12 }}>No recorded cameras.</p>}
          {nvrCams.map(c => {
            const reg = registry.find(x => x.slug === c.name);
            const live = reg?.health_status === 'connected';
            return (
              <div key={c.name} className={`lv-node${camera === c.name ? ' active' : ''}`}
                   onClick={() => onSelectCamera(c.name)}>
                <span style={{ width: 7, height: 7, borderRadius: '50%',
                               background: live ? 'var(--green)' : 'var(--dim)', flexShrink: 0 }} />
                <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {regName(c.name)}
                </span>
              </div>
            );
          })}
        </div>
        <div className="d-hint" style={{ marginTop: 12 }}>
          Click a camera to load its timeline. Footage is the NVR's continuous recording.
        </div>
      </div>
    </div>
  );
}
