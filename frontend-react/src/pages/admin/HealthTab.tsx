/**
 * HealthTab.tsx — the System health dashboard, on real data: node cards with
 * resource meters (appliance CPU/RAM, NVR disk + index lag, motion analysis
 * load, GPU utilization + VRAM + NVDEC/NVENC), a "Recording & analysis" panel
 * (channels recording, channels with issues), core-service dots, and an
 * Observability card linking the live API reference.
 * Every probe is independent — one dead service never blanks the tab.
 */
import { useEffect, useState, type ReactNode } from 'react';
import { apiFetch } from '@/lib/api';
import type { SystemHealth } from '@/lib/types';

interface NvrHealth {
  status: string;
  disk: { path: string; used_pct: number; used_gb: number; free_gb: number; total_gb: number; state: string };
  clip_extraction: { slots_available: number; slots_total: number };
}
interface MotionHealth {
  status: string;
  total_cameras: number;
  by_state: Record<string, number>;
  estimated_decode_cores: number;
  available_cpu_cores: number;
}
interface NvrWorker {
  name: string;
  recording: boolean;
  index_lag_seconds: number | null;
}
/** One physical GPU as nvidia-smi reports it; every field is null when the
 *  card doesn't expose that counter (consumer parts have no power telemetry). */
interface GpuDevice {
  index: number;
  name: string;
  pct: number | null;
  mem_used_mb: number | null;
  mem_total_mb: number | null;
  mem_pct: number | null;
  decoder_pct: number | null;
  encoder_pct: number | null;
  temp_c: number | null;
  power_w: number | null;
  power_limit_w: number | null;
}
interface GpuState { present: boolean; count: number; pct: number | null; devices: GpuDevice[] }
interface Probes {
  sys: (SystemHealth & { host?: { hostname: string; cores: number; load1: number; cpu_pct: number; mem_pct: number | null; mem_total_gb: number | null; gpu?: GpuState } | null }) | null;
  nvr: NvrHealth | null;
  motion: MotionHealth | null;
  workers: NvrWorker[];
  eventsLastHour: number | null;
}

const LAG_ISSUE_S = 180; // 3× the 60s segment duration — the NVR's own staleness rule
const VRAM_ISSUE_PCT = 90; // past this, the next model load or decode session OOMs

/** The GPU worth showing on a one-card summary is the one closest to its
 *  limit, and VRAM is the limit that actually bites — an appliance runs out of
 *  memory for another decode session long before the SMs saturate. */
function busiestGpu(g: GpuState | undefined): GpuDevice | null {
  if (!g?.devices?.length) return null;
  return g.devices.reduce((a, b) =>
    (b.mem_pct ?? b.pct ?? 0) > (a.mem_pct ?? a.pct ?? 0) ? b : a);
}

const gb = (mb: number) => Math.round(mb / 1024 * 10) / 10;

type NodeState = 'healthy' | 'high load' | 'down' | 'unknown';

function meterColor(pct: number): string {
  return pct >= 90 ? 'var(--red)' : pct >= 70 ? 'var(--yellow)' : 'var(--green)';
}

/** Caption + reading above a bar. The reading is coloured, not the caption —
 *  so a wall of these stays calm until something actually needs attention. */
function Meter({ label, pct, right }: { label: string; pct: number | null; right: string }) {
  return (
    <div className="meter-row">
      <div className="cap">
        <span style={{ color: 'var(--muted)' }}>{label}</span>
        <b style={{ color: pct != null ? meterColor(pct) : 'var(--dim)' }}>{right}</b>
      </div>
      <span className="meter-track" style={{ width: '100%', height: 6, display: 'block' }}>
        <span className="meter-fill" style={{
          width: `${Math.min(100, pct ?? 0)}%`,
          background: pct != null ? meterColor(pct) : 'var(--track)',
        }} />
      </span>
    </div>
  );
}

const STATE_COLOR: Record<NodeState, string> = {
  healthy: 'var(--green)', 'high load': 'var(--yellow)', down: 'var(--red)', unknown: 'var(--dim)',
};

function NodeCard({ name, sub, state, children }: {
  name: string; sub: string; state: NodeState; children: ReactNode;
}) {
  const col = STATE_COLOR[state];
  return (
    <div className="stat" data-tint="" style={{ '--tint': col, padding: 'var(--s5)' } as never}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 'var(--s3)' }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 650, letterSpacing: '-.012em' }}>{name}</div>
          <div style={{ fontSize: 10.5, color: 'var(--dim)', fontFamily: 'var(--mono)', marginTop: 2 }}>{sub}</div>
        </div>
        <span style={{
          display: 'inline-flex', alignItems: 'center', gap: 5, flexShrink: 0, whiteSpace: 'nowrap',
          fontSize: 10.5, fontWeight: 650, letterSpacing: '.05em', textTransform: 'uppercase',
          color: col, background: `color-mix(in srgb, ${col} 11%, transparent)`,
          border: `1px solid color-mix(in srgb, ${col} 24%, transparent)`,
          borderRadius: 999, padding: '3px 9px',
        }}>
          <span style={{ width: 5, height: 5, borderRadius: '50%', background: col }} />
          {state}
        </span>
      </div>
      <div style={{ marginTop: 'var(--s2)' }}>{children}</div>
    </div>
  );
}

function KV({ k, v, col }: { k: string; v: ReactNode; col?: string }) {
  return (
    <div className="kv-row">
      <span>{k}</span>
      <span style={col ? { color: col } : undefined}>{v}</span>
    </div>
  );
}

/** Core dependency indicator — a dot alone can't say which service is down. */
function Service({ name, ok }: { name: string; ok: boolean | undefined | null }) {
  const col = ok == null ? 'var(--dim)' : ok ? 'var(--green)' : 'var(--red)';
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: 'var(--text2)',
      background: 'var(--surface2)', border: '1px solid var(--border)',
      borderRadius: 999, padding: '3px 10px',
    }} title={ok == null ? `${name}: unknown` : ok ? `${name}: reachable` : `${name}: UNREACHABLE`}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: col, flexShrink: 0 }} />
      {name}
    </span>
  );
}

/** Loading shape that matches the loaded shape — three node cards, two panels. */
function HealthSkeleton() {
  return (
    <div>
      <div className="stat-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))' }}>
        {[0, 1, 2].map(i => (
          <div key={i} className="stat" style={{ padding: 'var(--s5)' }}>
            <div className="skel-stack">
              <span className="skel" style={{ width: 110, height: 14 }} />
              <span className="skel" style={{ width: 150, height: 9 }} />
            </div>
            <div style={{ marginTop: 'var(--s5)' }} className="skel-stack">
              <span className="skel" style={{ height: 6 }} />
              <span className="skel" style={{ height: 6, width: '100%' }} />
              <span className="skel" style={{ height: 6, width: '100%' }} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function HealthTab() {
  const [p, setP] = useState<Probes | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      const since = new Date(Date.now() - 3600_000);
      const [sys, nvr, motion, workers, events] = await Promise.all([
        fetch('/health').then(r => (r.ok ? r.json() : null)).catch(() => null),
        apiFetch<NvrHealth>('/nvr/health').catch(() => null),
        apiFetch<MotionHealth>('/motion/health').catch(() => null),
        apiFetch<{ cameras: NvrWorker[] }>('/nvr/cameras').then(d => d.cameras || []).catch(() => [] as NvrWorker[]),
        apiFetch<{ events: { started_at: string }[] }>('/motion/events?limit=1000')
          .then(d => (d.events || []).filter(e => new Date(e.started_at) > since).length)
          .catch(() => null),
      ]);
      if (alive) setP({ sys, nvr, motion, workers, eventsLastHour: events });
    })();
    return () => { alive = false; };
  }, []);

  if (!p) return <HealthSkeleton />;

  const { sys, nvr, motion, workers } = p;
  const host = sys?.host;
  const maxLag = workers.length ? Math.max(...workers.map(w => w.index_lag_seconds ?? 0)) : null;
  const recordingCount = workers.filter(w => w.recording).length;
  const issueCount = workers.filter(w => (w.index_lag_seconds ?? 0) > LAG_ISSUE_S).length
    + (sys ? sys.disconnected_streams : 0);
  const motionLoadPct = motion ? Math.min(100, (motion.estimated_decode_cores / Math.max(motion.available_cpu_cores, 1)) * 100) : null;
  const gpu = host?.gpu;
  const dev = busiestGpu(gpu);
  // VRAM pressure is the headline: a full card fails the next allocation even
  // while utilization reads low, so it alone can push the node out of healthy.
  const gpuState: NodeState =
    (dev?.mem_pct ?? 0) >= VRAM_ISSUE_PCT || (dev?.pct ?? 0) >= 90 ? 'high load' : 'healthy';

  return (
    <div>
      {/* Node cards — one per service, each with its own state so a dead probe
          greys out exactly one card instead of blanking the tab. */}
      <div className="stat-grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))' }}>
        <NodeCard name="vms-core" sub={host ? `${host.hostname} · ${host.cores} cores` : 'this appliance'}
          state={sys ? (sys.status === 'ok' ? 'healthy' : 'high load') : 'down'}>
          <Meter label="CPU (1-min load)" pct={host?.cpu_pct ?? null} right={host ? `${host.cpu_pct}%` : '—'} />
          <Meter label="RAM" pct={host?.mem_pct ?? null}
            right={host?.mem_pct != null ? `${host.mem_pct}%${host.mem_total_gb ? ` of ${host.mem_total_gb} GB` : ''}` : '—'} />
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 'var(--s4)' }}>
            <Service name="Postgres" ok={sys?.postgres_reachable} />
            <Service name="Redis" ok={sys?.redis_reachable} />
            <Service name="MediaMTX" ok={sys?.mediamtx_reachable} />
          </div>
        </NodeCard>

        <NodeCard name="recording (NVR)" sub={nvr ? nvr.disk.path : 'nvr:8009'}
          state={nvr ? (nvr.disk.state === 'ok' ? 'healthy' : 'high load') : 'down'}>
          <Meter label="Recording disk" pct={nvr?.disk.used_pct ?? null}
            right={nvr ? `${nvr.disk.used_pct}% · ${nvr.disk.free_gb} GB free` : '—'} />
          <Meter label="Index lag (worst camera)" pct={maxLag != null ? (maxLag / LAG_ISSUE_S) * 100 : null}
            right={maxLag != null ? `${Math.round(maxLag)}s` : '—'} />
          <div style={{ marginTop: 'var(--s4)', fontSize: 11.5, color: 'var(--muted)' }}>
            Clip transcode slots ·{' '}
            <b style={{ fontFamily: 'var(--mono)', color: 'var(--text2)', fontWeight: 600 }}>
              {nvr ? `${nvr.clip_extraction.slots_available}/${nvr.clip_extraction.slots_total}` : '—'}
            </b> free
          </div>
        </NodeCard>

        {/* Analysis (motion) card hidden per updated UI — re-enable to surface
            motion decode load and triggered-camera counts.
        <NodeCard name="analysis (motion)" sub="motion:8012 · CPU-only"
          state={motion ? (motionLoadPct != null && motionLoadPct >= 70 ? 'high load' : 'healthy') : 'down'}>
          <Meter label="Decode load" pct={motionLoadPct}
            right={motion ? `~${motion.estimated_decode_cores} of ${motion.available_cpu_cores} cores` : '—'} />
          <div style={{ marginTop: 'var(--s3)' }}>
            <KV k="Cameras analysed" v={motion ? motion.total_cameras : '—'} />
            <KV k="Currently triggered" v={motion ? (motion.by_state.TRIGGERED ?? 0) : '—'}
              col={motion && (motion.by_state.TRIGGERED ?? 0) > 0 ? 'var(--red)' : undefined} />
          </div>
        </NodeCard>
        */}

        {/* GPU — omitted entirely on a CPU-only appliance, the same rule the
            topbar gauge follows. present with no readable device is a fault:
            a card is installed and the driver won't answer. */}
        {gpu?.present && (
          <NodeCard
            name="acceleration (GPU)"
            sub={dev
              ? `${dev.name}${gpu.count > 1 ? ` · GPU ${dev.index} of ${gpu.count}` : ''}`
              : 'driver not responding'}
            state={!dev ? 'down' : gpuState}>
            <Meter label="GPU utilization" pct={dev?.pct ?? null}
              right={dev?.pct != null ? `${Math.round(dev.pct)}%` : '—'} />
            <Meter label="VRAM" pct={dev?.mem_pct ?? null}
              right={dev?.mem_pct != null && dev.mem_used_mb != null && dev.mem_total_mb != null
                ? `${dev.mem_pct}% · ${gb(dev.mem_total_mb - dev.mem_used_mb)} GB free`
                : '—'} />
            <div style={{ marginTop: 'var(--s3)' }}>
              <KV k="Decode (NVDEC)" v={dev?.decoder_pct != null ? `${Math.round(dev.decoder_pct)}%` : '—'}
                col={dev?.decoder_pct != null ? meterColor(dev.decoder_pct) : undefined} />
              <KV k="Encode (NVENC)" v={dev?.encoder_pct != null ? `${Math.round(dev.encoder_pct)}%` : '—'} />
            </div>
            {dev && (dev.temp_c != null || dev.power_w != null) && (
              <div style={{ marginTop: 'var(--s4)', fontSize: 11.5, color: 'var(--muted)' }}>
                {dev.temp_c != null && (
                  <>Temp · <b style={{ fontFamily: 'var(--mono)', color: 'var(--text2)', fontWeight: 600 }}>{Math.round(dev.temp_c)} °C</b></>
                )}
                {dev.temp_c != null && dev.power_w != null && ' · '}
                {dev.power_w != null && (
                  <>Power · <b style={{ fontFamily: 'var(--mono)', color: 'var(--text2)', fontWeight: 600 }}>
                    {Math.round(dev.power_w)}{dev.power_limit_w != null ? ` / ${Math.round(dev.power_limit_w)}` : ''} W
                  </b></>
                )}
              </div>
            )}
          </NodeCard>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))', gap: 'var(--s3)', alignItems: 'start' }}>
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head">
            <div className="panel-head-l">
              <div>
                <div className="panel-title">Recording &amp; analysis</div>
                <div className="panel-sub">Fleet-wide capture state.</div>
              </div>
            </div>
            <div className="panel-actions">
              {issueCount > 0
                ? <span className="badge badge-red"><span className="badge-dot" />{issueCount} issue{issueCount === 1 ? '' : 's'}</span>
                : <span className="badge badge-green"><span className="badge-dot" />All clear</span>}
            </div>
          </div>
          <div className="panel-body">
            <div className="kv">
              <KV k="Channels recording" v={sys ? `${recordingCount} / ${sys.enabled_cameras}` : `${recordingCount}`} />
              <KV k="Channels with issues" v={issueCount} col={issueCount > 0 ? 'var(--red)' : 'var(--green)'} />
              <KV k="Streams connected" v={sys ? `${sys.connected_streams} / ${sys.enabled_cameras}` : '—'}
                col={sys && sys.connected_streams < sys.enabled_cameras ? 'var(--yellow)' : 'var(--green)'} />
              {/* Worst-camera index lag lives on the NVR card — not repeated here. */}
              <KV k="Motion events (last hour)" v={p.eventsLastHour ?? '—'} />
            </div>
          </div>
          <div className="panel-foot">
            Issues = streams offline or a recording index lagging &gt;{LAG_ISSUE_S}s — the NVR's own staleness alarm threshold.
          </div>
        </div>

        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head">
            <div className="panel-head-l">
              <div>
                <div className="panel-title">Observability</div>
                <div className="panel-sub">Deep-dive tooling for this appliance.</div>
              </div>
            </div>
          </div>
          <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s2)' }}>
            <a href="/docs" target="_blank" rel="noreferrer" className="wz-row" style={{ textDecoration: 'none', color: 'var(--text)' }}>
              <span style={{ flex: 1, fontSize: 13 }}>API reference — OpenAPI</span>
              <span style={{ color: 'var(--accent2)', fontSize: 14 }}>↗</span>
            </a>
            {/* Grafana/Loki/OTel rows removed: unclickable roadmap placeholders
                on a page an operator opens to answer "is it healthy right now". */}
          </div>
          <div className="panel-foot">
            Container logs meanwhile: <code>./vms logs -f api nvr motion</code>
          </div>
        </div>
      </div>
    </div>
  );
}
