/**
 * TimelapsePage.tsx — /timelapse: look through a camera's stills day by day,
 * make a video of any range, play and download it.
 *
 * Gated by `timelapse_view` (nav + every API route). Images and videos are
 * fetched with the bearer token (apiBlob) and shown from object URLs, because a
 * plain <img src> cannot carry the token — the same reason ConfigPage's
 * snapshot thumbnail does it this way. Object URLs are revoked when they leave
 * the screen, so scrubbing a long day doesn't hold every still in memory.
 *
 * Three tabs (?tab=): the stills and videos, the site's Default settings, and the
 * cameras whose settings are Changed from those defaults. The disk guard's pause
 * note sits above all three.
 */
import { LoadingSkeleton } from '@/components/LoadingSkeleton';
import { ViewTransition } from '@/components/ViewTransition';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useSearchParams } from 'react-router-dom';
import { apiBlob, apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { useToast } from '@/components/Toast';
import { evenly, fmtBytes, statusText, type TimelapsePause, type TimelapseRender, type TimelapseStatus } from './timelapseBits';
import { ChangedView, DefaultsView, PauseBanner } from './TimelapseSettingsViews';

interface CamRow { id: string; name: string; slug: string; enabled: boolean; frames: number; bytes: number;
                   last_captured_at: string | null; status: TimelapseStatus | null }
interface DayRow { day: string; frames: number; bytes: number; first: string | null; last: string | null }
interface FrameRow { id: number; captured_at: string; width: number; height: number; brightness: number; bytes: number }

const muted: CSSProperties = { fontSize: 11.5, color: 'var(--muted)' };

/** An authenticated image, revoked when it unmounts or changes. */
function useBlobUrl(path: string | null): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!path) { setUrl(null); return; }
    let alive = true;
    let obj: string | null = null;
    apiBlob(path).then(b => { if (!alive) return; obj = URL.createObjectURL(b); setUrl(obj); }).catch(() => setUrl(null));
    return () => { alive = false; if (obj) URL.revokeObjectURL(obj); };
  }, [path]);
  return url;
}

function Thumb({ frame, active, onClick }: { frame: FrameRow; active: boolean; onClick: () => void }) {
  const url = useBlobUrl(`/timelapse/frames/${frame.id}/image`);
  return (
    <button onClick={onClick} title={new Date(frame.captured_at).toLocaleTimeString()}
            style={{ padding: 0, border: active ? '2px solid var(--accent)' : '1px solid var(--border)', borderRadius: 6,
                     width: 96, height: 54, overflow: 'hidden', background: '#0a0e13', flexShrink: 0, cursor: 'pointer' }}>
      {url && <img src={url} alt="" style={{ width: '100%', height: '100%', objectFit: 'cover' }} />}
    </button>
  );
}

const localInput = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

function StillsView({ onPaused }: { onPaused: (p: TimelapsePause | null) => void }) {
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.timelapse_manage === true;
  const [params, setParams] = useSearchParams();
  const camId = params.get('cam');

  const [cams, setCams] = useState<CamRow[] | null>(null);
  const [days, setDays] = useState<DayRow[]>([]);
  const [day, setDay] = useState<string | null>(null);
  const [frames, setFrames] = useState<FrameRow[]>([]);
  const [idx, setIdx] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [renders, setRenders] = useState<TimelapseRender[]>([]);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [fps, setFps] = useState(24);
  const [overlay, setOverlay] = useState(true);
  const [deflicker, setDeflicker] = useState(false);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);

  useEffect(() => {
    apiFetch<{ cameras: CamRow[]; paused: TimelapsePause | null }>('/timelapse/cameras').then(r => {
      onPaused(r.paused);
      const list = r.cameras.filter(c => c.enabled || c.frames > 0);
      setCams(list);
      if (!camId && list.length) setParams({ cam: list[0].id }, { replace: true });
    }).catch((e: any) => { toast(e.message, 'err'); setCams([]); });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setDays([]); setDay(null); setFrames([]); setRenders([]);
    if (!camId) return;
    apiFetch<{ days: DayRow[] }>(`/timelapse/cameras/${camId}/days`).then(r => {
      setDays(r.days); setDay(r.days[0]?.day ?? null);
    }).catch((e: any) => toast(e.message, 'err'));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camId]);

  useEffect(() => {
    setFrames([]); setIdx(0); setPlaying(false);
    if (!camId || !day) return;
    apiFetch<{ frames: FrameRow[] }>(`/timelapse/cameras/${camId}/frames?day=${day}`).then(r => {
      setFrames(r.frames); setIdx(Math.max(0, r.frames.length - 1));
    }).catch((e: any) => toast(e.message, 'err'));
    const start = new Date(`${day}T00:00`), end = new Date(`${day}T23:59`);
    setFrom(localInput(start)); setTo(localInput(end));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camId, day]);

  const loadRenders = useCallback(() => {
    if (!camId) return;
    apiFetch<{ renders: TimelapseRender[] }>(`/timelapse/cameras/${camId}/renders`).then(r => setRenders(r.renders)).catch(() => {});
  }, [camId]);
  useEffect(() => { loadRenders(); }, [loadRenders]);
  // Poll only while something is being made.
  const busy = renders.some(r => r.status === 'queued' || r.status === 'running');
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(loadRenders, 3000);
    return () => window.clearInterval(t);
  }, [busy, loadRenders]);

  // Play the day's stills in the browser at 6 per second — a quick look, not a render.
  useEffect(() => {
    if (!playing || !frames.length) return;
    const t = window.setInterval(() => setIdx(i => (i + 1 >= frames.length ? (setPlaying(false), i) : i + 1)), 166);
    return () => window.clearInterval(t);
  }, [playing, frames.length]);

  const current = frames[idx];
  const bigUrl = useBlobUrl(current ? `/timelapse/frames/${current.id}/image` : null);
  const strip = useMemo(() => evenly(frames, 24), [frames]);
  const cam = cams?.find(c => c.id === camId);
  const prevVideo = useRef<string | null>(null);
  useEffect(() => () => { if (prevVideo.current) URL.revokeObjectURL(prevVideo.current); }, []);

  async function makeVideo() {
    try {
      await apiFetch(`/timelapse/cameras/${camId}/renders`, {
        method: 'POST',
        body: JSON.stringify({ start: new Date(from).toISOString(), end: new Date(to).toISOString(), fps, overlay, deflicker }),
      });
      toast('Video queued: it appears below when ready');
      loadRenders();
    } catch (e: any) { toast(e.message, 'err'); }
  }

  async function play(r: TimelapseRender) {
    try {
      const blob = await apiBlob(`/timelapse/renders/${r.id}/video`);
      if (prevVideo.current) URL.revokeObjectURL(prevVideo.current);
      prevVideo.current = URL.createObjectURL(blob);
      setVideoUrl(prevVideo.current);
    } catch (e: any) { toast(e.message, 'err'); }
  }

  async function download(r: TimelapseRender) {
    try {
      const blob = await apiBlob(`/timelapse/renders/${r.id}/video?download=true`);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `timelapse-${cam?.slug || 'camera'}-${r.range_start.slice(0, 10)}-${r.range_end.slice(0, 10)}.mp4`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
    } catch (e: any) { toast(e.message, 'err'); }
  }

  async function remove(r: TimelapseRender) {
    if (!window.confirm('Delete this video? The stills it was made from stay.')) return;
    try { await apiFetch(`/timelapse/renders/${r.id}`, { method: 'DELETE' }); loadRenders(); }
    catch (e: any) { toast(e.message, 'err'); }
  }

  if (cams === null) return <LoadingSkeleton label="Loading time-lapse cameras…" variant="cards" count={3} />;
  if (!cams.length) {
    return (
      <div className="fade"><div className="empty">
        No camera has time-lapse switched on yet. Turn it on for many cameras at once from Cameras (tick them, then
        “Time-lapse on”), or per camera under Cameras → Configure → Time-lapse.
      </div></div>
    );
  }

  return (
    <div className="fade" style={{ display: 'grid', gridTemplateColumns: '240px minmax(0, 1fr)', gap: 'var(--s5)' }}>
      <div className="panel" style={{ alignSelf: 'start' }}>
        <div className="panel-head"><div className="panel-head-l"><div className="panel-title">Cameras</div></div></div>
        <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          {cams.map(c => (
            <button key={c.id} className={c.id === camId ? 'btn-primary btn-sm' : 'btn-ghost btn-sm'}
                    style={{ textAlign: 'left', display: 'flex', flexDirection: 'column', alignItems: 'flex-start' }}
                    onClick={() => setParams({ cam: c.id })}>
              <span>{c.name}{c.enabled ? '' : ' (off)'}</span>
              <span style={{ ...muted, color: c.id === camId ? 'inherit' : undefined }}>{c.frames} stills · {fmtBytes(c.bytes)}</span>
            </button>
          ))}
        </div>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s4)', minWidth: 0 }}>
        <div className="panel">
          <div className="panel-head"><div className="panel-head-l"><div>
            <div className="panel-title">{cam?.name}</div>
            <div className="panel-sub">{statusText(cam?.status ?? null)}{cam?.last_captured_at ? ` · newest ${new Date(cam.last_captured_at).toLocaleString()}` : ''}</div>
          </div></div>
            <select value={day ?? ''} onChange={e => setDay(e.target.value)} aria-label="Day" style={{ maxWidth: 220 }}>
              {days.map(d => <option key={d.day} value={d.day}>{d.day} · {d.frames} stills</option>)}
            </select>
          </div>
          <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s3)' }}>
            {!frames.length ? <div className="empty">No stills on this day.</div> : (
              <>
                <div style={{ position: 'relative', background: '#0a0e13', borderRadius: 9, overflow: 'hidden', aspectRatio: '16 / 9' }}>
                  {bigUrl && <img src={bigUrl} alt={current ? `Still at ${current.captured_at}` : ''}
                                  style={{ width: '100%', height: '100%', objectFit: 'contain' }} />}
                  {current && <span style={{ position: 'absolute', left: 10, bottom: 8, background: 'rgba(0,0,0,.65)', color: '#fff',
                                             padding: '2px 8px', borderRadius: 6, fontSize: 12, fontFamily: 'var(--mono)' }}>
                    {new Date(current.captured_at).toLocaleString()} · {idx + 1}/{frames.length}
                  </span>}
                </div>
                <div style={{ display: 'flex', gap: 'var(--s3)', alignItems: 'center' }}>
                  <button className="btn-ghost btn-sm" onClick={() => { if (idx >= frames.length - 1) setIdx(0); setPlaying(p => !p); }}>
                    {playing ? '❚❚ Pause' : '▶ Play day'}
                  </button>
                  <input type="range" min={0} max={frames.length - 1} value={idx} style={{ flex: 1 }} aria-label="Scrub stills"
                         onChange={e => { setPlaying(false); setIdx(Number(e.target.value)); }} />
                </div>
                <div style={{ display: 'flex', gap: 6, overflowX: 'auto', paddingBottom: 4 }}>
                  {strip.map(f => (
                    <Thumb key={f.id} frame={f} active={f.id === current?.id} onClick={() => { setPlaying(false); setIdx(frames.indexOf(f)); }} />
                  ))}
                </div>
                {canManage && day && (
                  <button className="btn-subtle btn-sm" style={{ alignSelf: 'flex-start', color: 'var(--red)' }} onClick={async () => {
                    if (!window.confirm(`Delete all ${frames.length} stills of ${day}? Videos already made stay.`)) return;
                    try {
                      await apiFetch(`/timelapse/cameras/${camId}/frames?day=${day}`, { method: 'DELETE' });
                      setDays(ds => ds.filter(d => d.day !== day)); setDay(days.find(d => d.day !== day)?.day ?? null);
                    } catch (e: any) { toast(e.message, 'err'); }
                  }}>Delete this day's stills</button>
                )}
              </>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-head"><div className="panel-head-l"><div>
            <div className="panel-title">Make a video</div>
            <div className="panel-sub">Any range, across days. A very long range is sped up to fit, never refused.</div>
          </div></div></div>
          <div className="panel-body" style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--s3)', alignItems: 'flex-end' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}><span style={muted}>From</span>
              <input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} /></label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}><span style={muted}>To</span>
              <input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} /></label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 4 }}><span style={muted}>Frame rate</span>
              <select value={fps} onChange={e => setFps(Number(e.target.value))}><option value={24}>24 fps</option><option value={30}>30 fps</option></select></label>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={overlay} onChange={e => setOverlay(e.target.checked)} />Name and time</label>
            <label style={{ display: 'flex', gap: 6, alignItems: 'center' }}><input type="checkbox" checked={deflicker} onChange={e => setDeflicker(e.target.checked)} />Smooth flicker</label>
            <button className="btn-primary btn-sm" disabled={!from || !to} onClick={makeVideo}>Make video</button>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head"><div className="panel-head-l"><div className="panel-title">Videos</div></div></div>
          <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s2)' }}>
            {videoUrl && <video src={videoUrl} controls autoPlay style={{ width: '100%', borderRadius: 9, background: '#000' }} />}
            {!renders.length && <div style={muted}>No videos yet.</div>}
            {renders.map(r => (
              <div key={r.id} style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--s3)', alignItems: 'center',
                                       borderTop: '1px solid var(--border)', paddingTop: 8 }}>
                <span style={{ minWidth: 220 }}>
                  {new Date(r.range_start).toLocaleString()} → {new Date(r.range_end).toLocaleString()}
                  <span style={{ ...muted, display: 'block' }}>
                    {r.kind === 'daily' ? 'Nightly' : `By ${r.requested_by || 'someone'}`} · {r.frames} stills · {r.fps} fps
                    {r.duration_s ? ` · ${r.duration_s} s` : ''}{r.bytes ? ` · ${fmtBytes(r.bytes)}` : ''}
                  </span>
                </span>
                {r.status === 'done' && <>
                  <button className="btn-ghost btn-sm" onClick={() => play(r)}>▶ Play</button>
                  <button className="btn-ghost btn-sm" onClick={() => download(r)}>Download</button>
                </>}
                {(r.status === 'queued' || r.status === 'running') && (
                  <span style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 160 }}>
                    <span style={muted}>{r.status === 'queued' ? 'Waiting…' : 'Rendering'}</span>
                    <progress value={r.progress} max={1} style={{ flex: 1 }} />
                  </span>
                )}
                {r.status === 'failed' && <span style={{ color: 'var(--red)', fontSize: 12 }}>Failed: {r.error}</span>}
                {canManage && r.status !== 'running' && (
                  <button className="btn-subtle btn-sm" style={{ marginLeft: 'auto' }} onClick={() => remove(r)}>Delete</button>
                )}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

type Tab = 'stills' | 'defaults' | 'changed';
const TABS: [Tab, string][] = [['stills', 'Stills and videos'], ['defaults', 'Default settings'], ['changed', 'Changed per camera']];

export function TimelapsePage() {
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.timelapse_manage === true;
  // Camera configuration is its own grant (camera_view): "Edit" opens a camera's Time-lapse tab there.
  const canOpenCamera = isAdmin || me?.permissions?.camera_view === true;
  const [params, setParams] = useSearchParams();
  const tab = (TABS.some(([k]) => k === params.get('tab')) ? params.get('tab') : 'stills') as Tab;
  const [paused, setPaused] = useState<TimelapsePause | null>(null);
  // Bumped after a settings change or a resume, so the tab on screen re-reads.
  const [rev, setRev] = useState(0);
  const refreshPause = useCallback(() => {
    apiFetch<{ paused: TimelapsePause | null }>('/timelapse/cameras').then(r => setPaused(r.paused)).catch(() => {});
  }, []);
  useEffect(() => { refreshPause(); }, [refreshPause, rev]);
  const changed = () => setRev(r => r + 1);
  const go = (t: Tab) => {
    const next = new URLSearchParams(params);
    if (t === 'stills') next.delete('tab'); else next.set('tab', t);
    setParams(next);
  };

  return (
    <ViewTransition view={tab}>
      <div className="tabs page-tabs">
        {TABS.map(([k, l]) => <button type="button" key={k} aria-pressed={tab === k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => go(k)}>{l}</button>)}
      </div>
      <PauseBanner paused={paused} canManage={canManage} onChanged={changed} />
      {tab === 'stills' && <StillsView key={rev} onPaused={setPaused} />}
      {tab === 'defaults' && <DefaultsView key={rev} canManage={canManage} isAdmin={isAdmin} onChanged={changed} />}
      {tab === 'changed' && <ChangedView key={rev} canManage={canManage} canOpenCamera={canOpenCamera} onChanged={changed} />}
    </ViewTransition>
  );
}
