/**
 * HealthTab.tsx — the camera's availability dashboard.
 *
 * One fetch (`GET /cameras/{id}/uptime?hours=`) feeds everything: the headline
 * uptime figure, the stat tiles, a stacked column chart of how each bucket's
 * wall-clock split between up / down / no-data, an exact-resolution status
 * ribbon underneath it, and the outage log (which doubles as the table view of
 * the chart). Excel export of an arbitrary range lives in the same header.
 *
 * Why the columns and the ribbon are both here: the columns aggregate, so a
 * 40-second outage in a 30-day window is a sliver you can still read as a
 * percentage — but they lose *when* it happened inside the bucket. The ribbon
 * keeps full resolution (with a 1px floor on down spans, so no outage can
 * disappear between pixels) and the two are drawn on one shared time axis.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiDownload, apiFetch } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import { DateTimeField } from '@/components/DateTimeField';
import { dtLocal, fmtDuration, timeAgo } from '@/lib/format';
import { StatusBadge } from '../bits';

import {
  BAR_MAX, DowntimeChart, FILL, PAD_L, PAD_R, PAD_T, PLOT_H, RANGES, RIBBON_H,
  RIBBON_Y, SEG_GAP, STATE_LABEL, SVG_H, Tile, bucketize, incidentsOf, stateOf,
  topRounded, useWidth,
  type Bucket, type Incident, type State, type Tip, type UptimeData,
} from './healthChart';

export function HealthTab({ camera }: { camera: Camera }) {
  const toast = useToast();
  const [hours, setHours] = useState(24);
  const [data, setData] = useState<UptimeData | null>(null);
  const [err, setErr] = useState('');
  const [tip, setTip] = useState<Tip | null>(null);
  const [exportOpen, setExportOpen] = useState(false);
  const [expFrom, setExpFrom] = useState('');
  const [expTo, setExpTo] = useState('');
  const [exporting, setExporting] = useState(false);
  const [plotRef, plotW] = useWidth<HTMLDivElement>();

  useEffect(() => { setHours(24); setExportOpen(false); }, [camera.id]);

  useEffect(() => {
    let alive = true;
    setErr(''); setData(null); setTip(null);
    apiFetch<UptimeData>(`/cameras/${camera.id}/uptime?hours=${hours}`)
      .then(d => { if (alive) setData(d); })
      .catch(e => { if (alive) setErr(e.message); });
    return () => { alive = false; };
  }, [camera.id, hours]);

  const nBuckets = RANGES.find(r => r[0] === hours)?.[2] ?? 24;
  const buckets = useMemo(() => (data ? bucketize(data, nBuckets) : []), [data, nBuckets]);
  const incidents = useMemo(() => incidentsOf(data?.intervals ?? []), [data]);
  const worst = useMemo(
    () => [...incidents].sort((a, b) => (b.end - b.start) - (a.end - a.start)).slice(0, 3),
    [incidents]);

  const pct = data?.uptime_pct ?? null;
  const longest = incidents.reduce((m, iv) => Math.max(m, iv.end - iv.start), 0);
  const grade = pct == null ? null : pct >= 99.5 ? 'Excellent' : pct >= 99 ? 'Healthy' : pct >= 95 ? 'Degraded' : 'Unreliable';
  const gradeCol = pct == null ? 'var(--muted)' : pct >= 99 ? 'var(--ch-up)' : pct >= 95 ? 'var(--yellow)' : 'var(--ch-down)';

  const fmtTick = (t: number) => {
    const d = new Date(t);
    return hours <= 24
      ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  };

  function toggleExport() {
    const show = !exportOpen;
    setExportOpen(show);
    if (show) {
      const now = new Date();
      setExpTo(dtLocal(now));
      setExpFrom(dtLocal(new Date(now.getTime() - hours * 3600_000)));
    }
  }

  async function download() {
    if (!expFrom || !expTo) { toast('Pick both start and end times', 'err'); return; }
    const from = new Date(expFrom), to = new Date(expTo);
    if (from >= to) { toast('Start must be earlier than end', 'err'); return; }
    if (to.getTime() - from.getTime() > 92 * 24 * 3600_000) { toast('Window cannot exceed 92 days', 'err'); return; }
    setExporting(true);
    try {
      // Local wall-clock → UTC ISO; the server renders the report cells back in
      // its own local time, so what you pick is what the spreadsheet shows.
      const qs = `from=${encodeURIComponent(from.toISOString())}&to=${encodeURIComponent(to.toISOString())}&format=xlsx`;
      await apiDownload(`/cameras/${camera.id}/uptime?${qs}`, `uptime-${camera.slug}.xlsx`);
    } catch (e: any) {
      toast('Export failed: ' + e.message, 'err');
    }
    setExporting(false);
  }

  // ── Plot ──────────────────────────────────────────────────────────────────
  const innerW = Math.max(plotW - PAD_L - PAD_R, 10);
  const band = innerW / Math.max(nBuckets, 1);
  const barW = Math.min(BAR_MAX, Math.max(band - 5, 3));
  const t0 = data ? new Date(data.from).getTime() : 0;
  const t1 = data ? new Date(data.to).getTime() : 1;
  const xOf = (t: number) => PAD_L + ((t - t0) / Math.max(t1 - t0, 1)) * innerW;

  const ticks = data
    ? Array.from({ length: 7 }, (_, i) => t0 + ((t1 - t0) * i) / 6)
    : [];

  const columns = buckets.map((b, i) => {
    const total = b.up + b.down + b.void;
    const x = PAD_L + band * i + (band - barW) / 2;
    if (total <= 0) return { i, b, x, segs: [] as { s: State; y: number; h: number }[] };
    // Bottom-up stack: up, then down, then the monitoring gap on top.
    const order: State[] = ['up', 'down', 'void'];
    let acc = 0;
    const segs: { s: State; y: number; h: number }[] = [];
    for (const s of order) {
      const h = (b[s] / total) * PLOT_H;
      if (h > 0) segs.push({ s, y: PAD_T + PLOT_H - acc - h, h });
      acc += h;
    }
    return { i, b, x, segs };
  });

  const bucketTip = (c: typeof columns[number]): Tip => {
    const mon = c.b.up + c.b.down;
    const rows: [string, string][] = [
      ['Uptime', mon > 0 ? `${(c.b.up / mon * 100).toFixed(1)}%` : '—'],
      ['Up', c.b.up > 0 ? fmtDuration(c.b.up) : '—'],
      ['Down', c.b.down > 0 ? fmtDuration(c.b.down) : '—'],
    ];
    if (c.b.void > 0) rows.push(['No data', fmtDuration(c.b.void)]);
    return {
      x: c.x + barW / 2, y: PAD_T + PLOT_H - (c.segs.length ? PLOT_H : 0) - 6,
      title: `${fmtTick(c.b.t0)} — ${fmtTick(c.b.t1)}`, rows,
    };
  };

  return (
    <>
      {/* ── Headline + stat tiles ───────────────────────────────────────── */}
      <div className="ch-top">
        <div className="ch-panel ch-hero">
          <div>
            <div className="ch-lbl">Uptime · last {RANGES.find(r => r[0] === hours)?.[1]}</div>
            <div className="ch-hero-val" style={{ marginTop: 10 }}>
              {pct == null ? '—' : pct}{pct != null && <small>%</small>}
            </div>
          </div>
          <div style={{ marginTop: 14 }}>
            <span className="meter-track" style={{ width: '100%', height: 6, display: 'block' }}>
              <span className="meter-fill" style={{ width: `${pct ?? 0}%`, background: gradeCol }} />
            </span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 9, fontSize: 12 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: gradeCol, flexShrink: 0 }} />
              <b style={{ fontWeight: 600 }}>{grade ?? 'Never monitored'}</b>
              <span style={{ color: 'var(--muted)' }}>· of monitored time</span>
            </div>
          </div>
        </div>

        <div className="ch-tiles">
          <Tile label="Current state"
            value={<StatusBadge status={camera.health_status} enabled={camera.enabled} />}
            sub={camera.last_seen_at ? `Last seen ${timeAgo(new Date(camera.last_seen_at))}` : 'Never seen'} />
          <Tile label="Total downtime"
            value={data ? fmtDuration(data.down_seconds * 1000) : '—'}
            sub={data && data.unknown_seconds > 0 ? `${fmtDuration(data.unknown_seconds * 1000)} unmonitored` : 'Monitored window'} />
          <Tile label="Incidents"
            value={data ? incidents.length : '—'}
            sub={incidents.length > 0
              ? `Avg ${fmtDuration((data!.down_seconds / incidents.length) * 1000)}`
              : 'No dropouts'} />
          <Tile label="Longest incident"
            value={longest > 0 ? fmtDuration(longest) : '—'}
            sub={longest > 0 ? 'Worst single dropout' : 'Unbroken'} />
          <Tile label="Relay up" mono
            value={camera.ready_since ? fmtDuration(Date.now() - new Date(camera.ready_since).getTime()) : '—'}
            sub="Since stream became ready" />
        </div>
      </div>

      {/* ── Availability chart ──────────────────────────────────────────── */}
      <div className="ch-card">
        <div className="ch-card-hd">
          <div>
            <div style={{ fontSize: 14, fontWeight: 600 }}>Availability over time</div>
            <div className="ch-sub" style={{ marginTop: 2 }}>
              Each column is one {hours === 24 ? 'hour' : hours === 168 ? '6-hour block' : 'day'};
              the band below keeps every outage at full resolution.
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <div className="ch-seg">
              {RANGES.map(([h, label]) => (
                <button key={h} className={h === hours ? 'active' : ''} onClick={() => setHours(h)}>{label}</button>
              ))}
            </div>
            <button className="btn-ghost btn-sm" onClick={toggleExport}
              title="Download an uptime/downtime report as Excel">Export ⤓</button>
          </div>
        </div>

        {exportOpen && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap',
            margin: '12px 16px 0', padding: 12, background: 'var(--input)',
            border: '1px solid var(--border2)', borderRadius: 10, fontSize: 12,
          }}>
            <span style={{ color: 'var(--muted)' }}>From</span>
            {/* Split date + time — datetime-local's Chromium picker offers no
                clock, so an uptime report quietly started at midnight. */}
            <DateTimeField label="Report window start" value={expFrom} onChange={setExpFrom}
              inputStyle={dtInput} style={{ width: 250 }} />
            <span style={{ color: 'var(--muted)' }}>to</span>
            <DateTimeField label="Report window end" value={expTo} onChange={setExpTo}
              inputStyle={dtInput} style={{ width: 250 }} />
            <button className="btn-primary btn-sm" disabled={exporting} onClick={download}>
              {exporting ? 'Exporting…' : 'Download Excel'}
            </button>
            <span style={{ color: 'var(--muted)' }}>Max 92 days</span>
          </div>
        )}

        <div style={{ padding: '12px 16px 0' }}>
          <div className="ch-legend">
            <span><i style={{ background: FILL.up }} />Up</span>
            <span><i style={{ background: FILL.down }} />Down</span>
            <span><i style={{ background: FILL.void }} />No data (camera disabled or service restarted — excluded from uptime %)</span>
          </div>
        </div>

        <div className="ch-plot-wrap" ref={plotRef}>
          {err && <div className="empty" style={{ color: 'var(--red)', padding: '70px 20px' }}>{err}</div>}
          {!err && !data && <div className="empty" style={{ padding: '70px 20px' }}><span className="spinner" /></div>}
          {!err && data && plotW > 0 && (
            <svg width={plotW - 32} height={SVG_H} style={{ display: 'block', overflow: 'visible' }}
              role="img" aria-label={`Availability for ${camera.name}: ${pct ?? '—'}% uptime over the last ${hours} hours`}>
              {/* Gridlines — solid hairlines, one step off the surface */}
              {[0, 25, 50, 75, 100].map(v => {
                const y = PAD_T + PLOT_H - (v / 100) * PLOT_H;
                return (
                  <g key={v}>
                    <line x1={PAD_L} x2={PAD_L + innerW} y1={y} y2={y} stroke="var(--ch-grid)" strokeWidth={1} />
                    {v % 50 === 0 && (
                      <text x={PAD_L - 8} y={y + 3.5} textAnchor="end"
                        fill="var(--dim)" fontSize={10} style={{ fontVariantNumeric: 'tabular-nums' }}>{v}%</text>
                    )}
                  </g>
                );
              })}

              {/* Stacked columns — share of each bucket's wall-clock */}
              {columns.map(c => (
                <g key={c.i}>
                  {c.segs.map((s, k) => {
                    const isTop = k === c.segs.length - 1;
                    // The 2px surface gap lives at the TOP of every segment that
                    // has another one above it — never a stroke around the mark.
                    const h = isTop ? s.h : Math.max(s.h - SEG_GAP, 0.5);
                    const y = isTop ? s.y : s.y + (s.h - h);
                    return h < 1.2
                      ? <rect key={s.s} x={c.x} y={y} width={barW} height={Math.max(h, 1)} fill={FILL[s.s]} />
                      : <path key={s.s} d={topRounded(c.x, y, barW, h, isTop ? 4 : 2)} fill={FILL[s.s]} />;
                  })}
                </g>
              ))}

              {/* Exact-resolution status ribbon on the same time axis */}
              <rect x={PAD_L} y={RIBBON_Y} width={innerW} height={RIBBON_H} rx={6} fill="var(--ch-plot)" />
              <clipPath id="ch-ribbon-clip">
                <rect x={PAD_L} y={RIBBON_Y} width={innerW} height={RIBBON_H} rx={6} />
              </clipPath>
              <g clipPath="url(#ch-ribbon-clip)">
                {/* Two passes: down spans paint last with a 1px floor, so a
                    40-second outage in a 30-day window can never vanish. */}
                {([0, 1] as const).flatMap(pass =>
                  data.intervals
                    .filter(iv => (stateOf(iv.status) === 'down') === (pass === 1))
                    .map((iv, k) => {
                      const x = xOf(new Date(iv.start).getTime());
                      const w = xOf(new Date(iv.end).getTime()) - x;
                      return (
                        <rect key={`${pass}-${k}`} x={x} y={RIBBON_Y}
                          width={pass === 1 ? Math.max(w, 1) : w} height={RIBBON_H}
                          fill={FILL[stateOf(iv.status)]} />
                      );
                    }))}
              </g>

              {/* X axis */}
              {ticks.map((t, i) => (
                <text key={i} x={xOf(t)} y={RIBBON_Y + RIBBON_H + 15}
                  textAnchor={i === 0 ? 'start' : i === ticks.length - 1 ? 'end' : 'middle'}
                  fill="var(--dim)" fontSize={10} style={{ fontVariantNumeric: 'tabular-nums' }}>
                  {i === ticks.length - 1 ? 'now' : fmtTick(t)}
                </text>
              ))}

              {/* Hit targets — wider than the marks, per interaction.md */}
              {columns.map(c => (
                <rect key={c.i} x={PAD_L + band * c.i} y={PAD_T} width={band} height={PLOT_H}
                  fill="transparent" style={{ cursor: 'crosshair' }}
                  onMouseEnter={() => setTip(bucketTip(c))} onMouseLeave={() => setTip(null)} />
              ))}
              <rect x={PAD_L} y={RIBBON_Y} width={innerW} height={RIBBON_H} fill="transparent"
                style={{ cursor: 'crosshair' }}
                onMouseLeave={() => setTip(null)}
                onMouseMove={e => {
                  const r = (e.currentTarget as SVGRectElement).getBoundingClientRect();
                  const t = t0 + ((e.clientX - r.left) / Math.max(r.width, 1)) * (t1 - t0);
                  const iv = data.intervals.find(v =>
                    new Date(v.start).getTime() <= t && t < new Date(v.end).getTime());
                  if (!iv) return;
                  const s = stateOf(iv.status);
                  const ms = new Date(iv.end).getTime() - new Date(iv.start).getTime();
                  setTip({
                    x: xOf(t), y: RIBBON_Y - 6, title: STATE_LABEL[s],
                    rows: [
                      ['Started', new Date(iv.start).toLocaleString()],
                      ['Ended', new Date(iv.end).toLocaleString()],
                      ['Duration', fmtDuration(ms)],
                    ],
                  });
                }} />
            </svg>
          )}

          {tip && (
            <div className="ch-tip" style={{
              left: Math.min(Math.max(tip.x + 16, 90), Math.max(plotW - 90, 90)),
              top: Math.max(tip.y + 4, 8),
            }}>
              <b>{tip.title}</b>
              {tip.rows.map(([k, v]) => (
                <div className="ch-tip-row" key={k}><span>{k}</span><span>{v}</span></div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Downtime pattern ─────────────────────────────────────────────── */}
      <div className="ch-card" style={{ padding: '14px 16px 12px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 600 }}>Downtime pattern</div>
            <div className="ch-sub" style={{ marginTop: 2 }}>
              Time lost per {hours === 24 ? 'hour' : hours === 168 ? '6-hour block' : 'day'},
              on its own scale — short dropouts stay visible here even when they vanish in the chart above.
            </div>
          </div>
          <div className="ch-sub" style={{ marginTop: 0 }}>
            {data
              ? `${incidents.length} incident${incidents.length === 1 ? '' : 's'} · reconnects under a minute counted as one`
              : ''}
          </div>
        </div>

        {data && (
          <DowntimeChart buckets={buckets} incidents={incidents} fmtTick={fmtTick}
            spanLabel={RANGES.find(r => r[0] === hours)?.[1] ?? ''} />
        )}

        {worst.length > 0 && (
          <div style={{
            display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center',
            marginTop: 10, paddingTop: 12, borderTop: '1px solid var(--border)', fontSize: 12,
          }}>
            <span className="ch-lbl">Worst</span>
            {worst.map((iv, i) => {
              const ongoing = data != null && iv.end === new Date(data.to).getTime();
              return (
                <span key={i} style={{
                  display: 'inline-flex', alignItems: 'center', gap: 7,
                  background: 'var(--input)', border: '1px solid var(--border2)',
                  borderRadius: 20, padding: '4px 11px',
                }}>
                  <span style={{ width: 7, height: 7, borderRadius: 2, background: 'var(--ch-down)', flexShrink: 0 }} />
                  <span style={{ color: 'var(--muted)' }}>
                    {new Date(iv.start).toLocaleString([], {
                      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                    })}
                  </span>
                  <b style={{ fontWeight: 600 }}>
                    {ongoing ? `${fmtDuration(iv.end - iv.start)} · ongoing` : fmtDuration(iv.end - iv.start)}
                  </b>
                </span>
              );
            })}
            <span className="ch-sub" style={{ marginTop: 0 }}>Full per-incident detail is in the Excel export.</span>
          </div>
        )}
      </div>
    </>
  );
}

const dtInput: React.CSSProperties = {
  background: 'var(--surface)', color: 'var(--text)', border: '1px solid var(--border2)',
  borderRadius: 6, padding: '4px 6px', fontSize: 12, width: 'auto',
};
