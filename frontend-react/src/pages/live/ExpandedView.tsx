/**
 * ExpandedView.tsx — expanded single camera (legacy liveRenderFocus +
 * lvfRenderExtras + lvSnapshot): full feed with overlays, 300px info panel
 * (ONVIF facts, PTZ placeholder, AI detections, actions) and the 30-minute
 * footage-coverage strip with "Open in playback".
 */
import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiBlob, apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { up7Color } from '@/components/Sparkline';
import { useToast } from '@/components/Toast';
import type { Camera, NvrCoverage, UptimeEntry } from '@/lib/types';
import { Clock, LiveVideo } from './LiveVideo';

interface EncoderMeta { res: string; codec: string }
interface MotionEvent { started_at: string; ended_at: string | null }
type Span = [number, number];

// ONVIF resolution/codec, cached per camera for the session (legacy _lvMeta).
const metaCache: Record<string, EncoderMeta> = {};

// ── NVR coverage (legacy covSpans): covered = window ∩ [earliest, latest] minus gaps ──
const toMs = (v: number | string): number => (typeof v === 'number' ? v * 1000 : Date.parse(v));

export function covSpans(cov: NvrCoverage | null, winS: number, winE: number): Span[] {
  if (!cov || cov.earliest == null || cov.latest == null) return [];
  const s = Math.max(winS, toMs(cov.earliest));
  const e = Math.min(winE, toMs(cov.latest));
  if (e <= s) return [];
  let spans: Span[] = [[s, e]];
  for (const g of cov.gaps || []) {
    const gs = toMs(g.start), ge = toMs(g.end);
    spans = spans.flatMap(([a, b]): Span[] => {
      if (ge <= a || gs >= b) return [[a, b]];
      const out: Span[] = [];
      if (gs > a) out.push([a, gs]);
      if (ge < b) out.push([ge, b]);
      return out;
    });
  }
  return spans;
}

function CovBar({ spans, winS, winE }: { spans: Span[]; winS: number; winE: number }) {
  const span = winE - winS;
  return (
    <span style={{ position: 'relative', flex: 1, height: 8, borderRadius: 4,
                   background: 'var(--track)', overflow: 'hidden', display: 'block' }}>
      {spans.map(([a, b], i) => (
        <span key={i} style={{ position: 'absolute', top: 0, bottom: 0, borderRadius: 2, background: '#55657a',
          left: `${(((a - winS) / span) * 100).toFixed(2)}%`,
          width: `${Math.max(((b - a) / span) * 100, 0.4).toFixed(2)}%` }} />
      ))}
    </span>
  );
}

const SECTION: CSSProperties = {
  fontSize: 10.5, fontWeight: 600, letterSpacing: '.12em',
  textTransform: 'uppercase', color: 'var(--dim)', marginBottom: 10,
};

function Kv({ k, v, mono }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10, fontSize: 12 }}>
      <span style={{ color: 'var(--muted)' }}>{k}</span>
      <span style={{ fontFamily: mono ? 'var(--mono)' : undefined, color: 'var(--text2)', textAlign: 'right',
                     minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {v}
      </span>
    </div>
  );
}

export function ExpandedView({ cam, uptime, onPushWall }: {
  cam: Camera;
  uptime?: UptimeEntry;
  onPushWall: () => void;
}) {
  const nav = useNavigate();
  // Smart Search is capability-gated (and enforced server-side), so only offer
  // the handoff to someone who can actually use it.
  const { me, isAdmin } = useAuth();
  const canSearch = isAdmin || me?.permissions?.smart_search === true;
  const toast = useToast();
  const [meta, setMeta] = useState<EncoderMeta | null>(metaCache[cam.id] ?? null);
  const [events, setEvents] = useState<MotionEvent[] | null>(null);
  const [cov, setCov] = useState<{ spans: Span[]; winS: number; winE: number } | null>(null);

  // Best-effort resolution/codec over the ONVIF encoder (cached per camera).
  useEffect(() => {
    setMeta(metaCache[cam.id] ?? null);
    if (!cam.onvif_capable || metaCache[cam.id]) return;
    let alive = true;
    apiFetch<{ profiles?: { width?: number; height?: number; codec?: string; encoding?: string; fps?: number }[] }>(
      `/cameras/${cam.id}/encoder`,
    )
      .then(r => {
        const p = (r.profiles || [])[0] || {};
        metaCache[cam.id] = {
          res: p.width && p.height ? `${p.width}×${p.height}` : '—',
          codec: [p.codec || p.encoding, p.fps ? `${p.fps}fps` : null].filter(Boolean).join(' · ') || '—',
        };
        if (alive) setMeta(metaCache[cam.id]);
      })
      .catch(() => {
        metaCache[cam.id] = { res: '—', codec: '—' };
        if (alive) setMeta(metaCache[cam.id]);
      });
    return () => { alive = false; };
  }, [cam.id, cam.onvif_capable]);

  // Footage strip: last 30 minutes of NVR coverage (strip stays empty if NVR down).
  useEffect(() => {
    setCov(null);
    let alive = true;
    const to = new Date(), from = new Date(Date.now() - 30 * 60000);
    apiFetch<NvrCoverage>(
      `/nvr/coverage?camera=${encodeURIComponent(cam.slug)}&from=${encodeURIComponent(from.toISOString())}&to=${encodeURIComponent(to.toISOString())}`,
    )
      .then(c => {
        if (alive) setCov({ spans: covSpans(c, from.getTime(), to.getTime()), winS: from.getTime(), winE: to.getTime() });
      })
      .catch(() => { /* NVR down or no footage — strip stays empty */ });
    return () => { alive = false; };
  }, [cam.slug]);

  // Recent motion events (only when motion detection is on for this camera).
  useEffect(() => {
    setEvents(null);
    if (!cam.motion_detection) return;
    let alive = true;
    apiFetch<{ events?: MotionEvent[] }>(`/motion/events?camera=${encodeURIComponent(cam.slug)}&limit=3`)
      .then(r => { if (alive) setEvents(r.events || []); })
      .catch(() => { /* motion service unreachable */ });
    return () => { alive = false; };
  }, [cam.slug, cam.motion_detection]);

  async function snapshot() {
    try {
      const blob = await apiBlob(`/cameras/${cam.id}/snapshot`);
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${cam.slug}_${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (e: any) {
      toast(`Snapshot failed: ${e.message}`, 'err');
    }
  }

  const online = cam.health_status === 'connected';
  const res = meta?.res || (cam.onvif_capable ? '…' : '—');
  const codec = meta?.codec || (cam.onvif_capable ? '…' : '—');
  const pct = uptime?.pct;

  return (
    <>
      <div style={{ display: 'flex', gap: 14, minHeight: '64vh' }}>
        {/* Big feed */}
        <div style={{ flex: 1, minWidth: 0, position: 'relative', borderRadius: 10, overflow: 'hidden',
                      border: '1px solid var(--border)', background: '#0a0e13' }}>
          <LiveVideo slug={cam.slug} variant="focus" staggerMs={50}
                     hasSub={!!cam.sub_track?.url_raw} />
          <div style={{ position: 'absolute', left: 14, top: 14, display: 'flex', gap: 8 }}>
            {online && cam.recording !== false ? (
              <span className="lv-badge" style={{ padding: '4px 10px', fontSize: 10 }}>
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#EF4444', animation: 'blip 1.4s infinite' }} />
                RECORDING
              </span>
            ) : online ? (
              <span className="lv-badge" style={{ padding: '4px 10px', fontSize: 10 }}>
                <span style={{ width: 7, height: 7, borderRadius: '50%', background: '#22C55E' }} />
                LIVE
              </span>
            ) : (
              <span className="lv-badge" style={{ padding: '4px 10px', fontSize: 10, color: '#5b7186' }}>OFFLINE</span>
            )}
          </div>
          <div style={{ position: 'absolute', left: 14, bottom: 14, pointerEvents: 'none' }}>
            <div style={{ fontSize: 15, fontWeight: 600, color: '#fff', textShadow: '0 1px 3px rgba(0,0,0,.7)' }}>{cam.name}</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 11, color: '#DCE3EC', textShadow: '0 1px 3px rgba(0,0,0,.7)' }}>
              {cam.slug} · {res} · {codec} · <Clock />
            </div>
          </div>
        </div>

        {/* Info panel */}
        <div style={{ width: 300, flexShrink: 0, padding: '2px 0', overflow: 'auto' }}>
          <div style={SECTION}>Camera</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 7, marginBottom: 18 }}>
            <Kv k="Location" v={(cam.metadata || {}).zone || '—'} />
            <Kv k="IP" v={cam.ip || '—'} mono />
            <Kv k="Resolution" v={res} mono />
            <Kv k="Codec" v={codec} mono />
            <Kv k="Uptime 7d" v={pct != null ? <span style={{ color: up7Color(pct) }}>{pct}%</span> : '—'} />
            <Kv k="Installed" v={(cam.metadata || {}).installed_at || '—'} mono />
            <Kv k="Firmware" v={cam.firmware || '—'} mono />
          </div>

          <div style={SECTION}>PTZ control</div>
          <div style={{ display: 'flex', gap: 14, alignItems: 'center', opacity: 0.45, marginBottom: 18 }} title="Coming soon">
            <div className="ptz-pad">
              <div className="ptz-hub" />
              <span className="ptz-n">▲</span><span className="ptz-s">▼</span>
              <span className="ptz-w">◀</span><span className="ptz-e">▶</span>
            </div>
            <div style={{ fontSize: 11, color: 'var(--dim)' }}>
              PTZ arrives with the ONVIF PTZ service <span className="nav-soon" style={{ marginLeft: 0 }}>soon</span>
            </div>
          </div>

          <div style={SECTION}>Active AI detections</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 10 }}>
            {!cam.motion_detection ? (
              <span style={{ fontSize: 12, color: 'var(--dim)' }}>
                Motion detection is off — enable it in{' '}
                <a href="#" onClick={e => { e.preventDefault(); nav(`/cameras/config?cam=${cam.id}`); }}>AI Config</a>.
              </span>
            ) : events === null ? (
              <span style={{ fontSize: 12, color: 'var(--dim)' }}>Checking motion events…</span>
            ) : events.length === 0 ? (
              <span style={{ fontSize: 12, color: 'var(--dim)' }}>Monitoring — no recent motion events.</span>
            ) : (
              events.map((e, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, border: '1px solid rgba(99,102,241,.35)',
                                      borderLeft: '3px solid #6366f1', borderRadius: 7, padding: '6px 10px', fontSize: 12 }}>
                  <span style={{ flex: 1 }}>Motion detected</span>
                  <span style={{ fontFamily: 'var(--mono)', fontSize: 10.5, color: 'var(--muted)' }}>
                    {new Date(e.started_at).toLocaleTimeString([], { hour12: false })}{e.ended_at ? '' : ' · active'}
                  </span>
                </div>
              ))
            )}
          </div>
          {/* This used to be a dead button promising box-drawn person search
              "with Smart Search". Smart Search shipped — box-drawn search did
              not, and can't yet: the index is text→image (SigLIP) with no
              image-query route, so there is nothing to send a crop to. What
              does work is a described search scoped to this camera, so that's
              what the button now does. */}
          {canSearch && (
            <>
              <button className="btn-ghost btn-sm" style={{ width: '100%', marginBottom: 6 }}
                      onClick={() => nav(`/smartsearch?cam=${encodeURIComponent(cam.slug)}`)}>
                ⌖ Search people on this camera
              </button>
              <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 18 }}>
                Opens Smart Search filtered to this camera. Describe what someone looked like —
                appearance only, never a name.
              </div>
            </>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 6 }}>
            <button className="btn-ghost btn-sm" onClick={snapshot}>📷 Snapshot</button>
            <button className="btn-ghost btn-sm" style={{ opacity: 0.55, cursor: 'default' }}
                    title="Bookmarks are not available yet">🔖 Bookmark</button>
            <button className="btn-ghost btn-sm" onClick={() => nav(`/cameras/config?cam=${cam.id}`)}>▱ Masks</button>
            <button className="btn-ghost btn-sm" onClick={() => nav(`/cameras/config?cam=${cam.id}`)}>⚙ Configure</button>
          </div>
          <button className="btn-primary btn-sm" style={{ width: '100%' }} onClick={onPushWall}>Push to wall</button>
        </div>
      </div>

      {/* On-prem recording availability ("-30 min ··· now" strip) */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
        <button className="btn-primary btn-sm" onClick={() => nav(`/playback?cam=${encodeURIComponent(cam.slug)}`)}>
          ▸ Open in playback
        </button>
        <div style={{ flex: 1, display: 'flex', alignItems: 'center' }}>
          {cov ? (
            <CovBar spans={cov.spans} winS={cov.winS} winE={cov.winE} />
          ) : (
            <span style={{ position: 'relative', flex: 1, height: 8, borderRadius: 4, background: 'var(--track)', display: 'block' }} />
          )}
        </div>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--dim)', whiteSpace: 'nowrap' }}>−30 min ··· now</span>
      </div>
    </>
  );
}
