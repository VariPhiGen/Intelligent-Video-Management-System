/**
 * HistoryPanel — where PTZ cameras pointed, and who moved them (services/ptz_track), filterable and paged.
 *
 * One component for both places: a camera's PTZ tab (`cameraId` set — adds "where was it at…?" and the
 * time-at-each-preset bar) and the site-wide PTZ page (`cameras` set — adds a camera filter and a camera column,
 * for "what were all PTZ cameras doing between 02:00 and 02:30?"). Filtering happens on the server (90 days of
 * change rows can be thousands); CSV downloads every row the filters match.
 */
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiDownload } from '@/lib/api';
import { useToast } from '@/components/Toast';
import { Pager } from '@/components/Pager';
import {
  CAUSE_OPTIONS, RANGES, STATE_OPTIONS, causeText, fmtDuration, historyParams, ptzApi, useZoomText,
  type HistoryQuery, type PtzPositionRow, type TimeSplit,
} from '@/lib/ptz';
import type { PtzCapabilities, PtzPreset } from '@/lib/types';

const PAGE = 50;
const STATE_WORD: Record<string, string> = Object.fromEntries(STATE_OPTIONS.map(o => [o.value, o.label]));
const SPLIT_COLOURS = ['#3b82f6', '#22c55e', '#f59e0b', '#a855f7', '#06b6d4', '#ef4444', '#84cc16', '#ec4899'];

function localInput(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function Multi({ label, options, value, onChange }: {
  label: string; options: { value: string; label: string }[]; value: string[]; onChange: (v: string[]) => void;
}) {
  return (
    <span style={{ display: 'inline-flex', gap: 4, alignItems: 'center', flexWrap: 'wrap' }}>
      <span style={{ fontSize: 11.5, color: 'var(--dim)' }}>{label}</span>
      {options.map(o => {
        const on = value.includes(o.value);
        return (
          <span key={o.value} className={`chip-toggle${on ? ' on' : ''}`} role="checkbox" aria-checked={on} tabIndex={0}
                onClick={() => onChange(on ? value.filter(v => v !== o.value) : [...value, o.value])}
                onKeyDown={e => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); onChange(on ? value.filter(v => v !== o.value) : [...value, o.value]); } }}>
            {o.label}
          </span>
        );
      })}
    </span>
  );
}

export function HistoryPanel({ cameraId, caps, cameras, presets }: {
  cameraId?: string;
  /** one camera's capabilities: zoom shown as × / % (useZoomText); the all-cameras view keeps the raw number */
  caps?: PtzCapabilities | null;
  cameras?: { id: string; name: string }[];
  presets?: PtzPreset[] | null;
}) {
  const zoomFmt = useZoomText(cameraId, caps);
  const toast = useToast();
  const nav = useNavigate();
  const [range, setRange] = useState('24h');
  const [from, setFrom] = useState(() => localInput(new Date(Date.now() - 86400e3)));
  const [to, setTo] = useState(() => localInput(new Date()));
  const [states, setStates] = useState<string[]>([]);
  const [causes, setCauses] = useState<string[]>([]);
  const [preset, setPreset] = useState('');
  const [actorRaw, setActorRaw] = useState('');
  const [actor, setActor] = useState('');
  const [camIds, setCamIds] = useState<string[]>([]);
  const [page, setPage] = useState(0);
  const [data, setData] = useState<{ items: PtzPositionRow[]; total: number } | null>(null);
  const [split, setSplit] = useState<TimeSplit[] | null>(null);
  const [atInput, setAtInput] = useState(() => localInput(new Date(Date.now() - 3600e3)));
  const [atResult, setAtResult] = useState<PtzPositionRow | null | undefined>(undefined);
  const [tick, setTick] = useState(0);

  useEffect(() => { const h = setTimeout(() => setActor(actorRaw.trim()), 300); return () => clearTimeout(h); }, [actorRaw]);

  const window_ = useMemo(() => {
    const r = RANGES.find(x => x.key === range);
    if (r?.hours) return { since: new Date(Date.now() - r.hours * 3600e3).toISOString(), until: undefined as string | undefined };
    return { since: from ? new Date(from).toISOString() : undefined, until: to ? new Date(to).toISOString() : undefined };
    // tick: "Refresh" re-anchors "last N hours" to now
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [range, from, to, tick]);

  const query: HistoryQuery = useMemo(() => ({
    ...window_, state: states, cause: causes, preset: preset || undefined, actor: actor || undefined,
    camera_ids: cameraId ? undefined : camIds, offset: page * PAGE, limit: PAGE,
  }), [window_, states, causes, preset, actor, camIds, page, cameraId]);

  useEffect(() => { setPage(0); }, [window_, states, causes, preset, actor, camIds]);

  useEffect(() => {
    let live = true;
    const req = cameraId ? ptzApi.history(cameraId, query) : ptzApi.historyAll(query);
    req.then(r => { if (live) setData({ items: r.items || [], total: r.total ?? 0 }); })
      .catch((e: any) => { if (live) { setData({ items: [], total: 0 }); toast(e.message, 'err'); } });
    return () => { live = false; };
  }, [cameraId, query, toast]);

  useEffect(() => {
    if (!cameraId || !window_.since) { setSplit(null); return; }
    let live = true;
    ptzApi.summary(cameraId, window_.since, window_.until).then(r => { if (live) setSplit(r.split); }).catch(() => {});
    return () => { live = false; };
  }, [cameraId, window_]);

  async function csv() {
    const path = cameraId
      ? `/cameras/${cameraId}/ptz/positions?${historyParams({ ...query }, 'csv')}`
      : `/cameras/ptz/history?${historyParams({ ...query }, 'csv')}`;
    try { await apiDownload(path, 'ptz-position-history.csv'); } catch (e: any) { toast(e.message, 'err'); }
  }

  async function lookUp() {
    if (!cameraId || !atInput) return;
    try { setAtResult((await ptzApi.positionAt(cameraId, new Date(atInput).toISOString())).position); }
    catch (e: any) { toast(e.message, 'err'); }
  }

  const presetOptions = (presets || []).map(p => ({ value: p.token, label: p.name || p.token }));

  return (
    <div>
      <div className="list-toolbar">
        <select value={range} onChange={e => setRange(e.target.value)} aria-label="Time range">
          {RANGES.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}
        </select>
        {range === 'custom' && (
          <>
            <input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} aria-label="From" />
            <span style={{ color: 'var(--dim)' }}>to</span>
            <input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} aria-label="To" />
          </>
        )}
        {cameraId && presetOptions.length > 0 && (
          <select value={preset} onChange={e => setPreset(e.target.value)} aria-label="Preset">
            <option value="">Any preset</option>
            {presetOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        )}
        {cameras && (
          <select value={camIds[0] || ''} onChange={e => setCamIds(e.target.value ? [e.target.value] : [])} aria-label="Camera">
            <option value="">All PTZ cameras</option>
            {cameras.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        )}
        <input type="search" value={actorRaw} onChange={e => setActorRaw(e.target.value)} placeholder="Moved by (user)…" />
        <div style={{ flex: 1 }} />
        <button className="btn-subtle btn-sm" onClick={() => setTick(t => t + 1)}>↻ Refresh</button>
        <button className="btn-subtle btn-sm" onClick={csv} disabled={!data?.total}>↓ CSV</button>
      </div>
      <div className="list-toolbar">
        <Multi label="Where" options={STATE_OPTIONS} value={states} onChange={setStates} />
      </div>
      <div className="list-toolbar">
        <Multi label="Moved by" options={CAUSE_OPTIONS} value={causes} onChange={setCauses} />
      </div>

      {cameraId && split && split.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 11.5, color: 'var(--dim)' }}>Time spent in this period</div>
          <div className="split-bar">
            {split.map((s, i) => (
              <span key={s.key} title={`${s.label}: ${fmtDuration(s.seconds)} (${s.pct}%)`}
                    style={{ width: `${s.pct}%`, background: s.key === 'no_data' ? 'transparent'
                      : s.key === 'off_preset' ? '#94a3b8' : s.key === 'moving' ? '#64748b' : SPLIT_COLOURS[i % SPLIT_COLOURS.length] }} />
            ))}
          </div>
          <div className="split-legend">
            {split.map((s, i) => (
              <span key={s.key}>
                <i style={{ background: s.key === 'no_data' ? 'var(--border2)' : s.key === 'off_preset' ? '#94a3b8'
                  : s.key === 'moving' ? '#64748b' : SPLIT_COLOURS[i % SPLIT_COLOURS.length] }} />
                {s.key.startsWith('preset:') ? '📍 ' : ''}{s.label} {s.pct}% · {fmtDuration(s.seconds)}
              </span>
            ))}
          </div>
        </div>
      )}

      {cameraId && (
        <div className="list-toolbar" style={{ background: 'var(--surface2)', padding: '8px 10px', borderRadius: 8 }}>
          <span style={{ fontSize: 12 }}>Where was it pointing at</span>
          <input type="datetime-local" value={atInput} onChange={e => setAtInput(e.target.value)} aria-label="Moment" />
          <button className="btn-subtle btn-sm" onClick={lookUp}>Look up</button>
          {atResult === null && <span style={{ fontSize: 12, color: 'var(--dim)' }}>No record that far back.</span>}
          {atResult && (
            <span style={{ fontSize: 12 }}>
              {atResult.state === 'at_preset' ? `📍 ${atResult.preset_name || atResult.preset_token}` : STATE_WORD[atResult.state]}
              <span style={{ color: 'var(--dim)' }}> since {new Date(atResult.at).toLocaleString()}
                {causeText(atResult.cause, atResult.actor) ? ` · moved by ${causeText(atResult.cause, atResult.actor)}` : ''}</span>
              {' '}
              <a href="#" onClick={e => { e.preventDefault();
                nav(`/playback?cam=${encodeURIComponent(cameraId)}&t=${Math.floor(new Date(atInput).getTime() / 1000)}`); }}>
                ▶ recording at that moment
              </a>
            </span>
          )}
        </div>
      )}

      {data === null ? <div style={{ fontSize: 12, color: 'var(--dim)' }}>Loading…</div>
        : data.items.length === 0 ? <div style={{ fontSize: 12, color: 'var(--dim)' }}>No changes match these filters.</div>
        : (
          <table className="tbl" style={{ fontSize: 12 }}>
            <thead><tr>
              <th>When</th>{!cameraId && <th>Camera</th>}<th>Where</th><th>Moved by</th><th>Pan / tilt</th><th>Zoom</th>
              {!cameraId && <th />}
            </tr></thead>
            <tbody>
              {data.items.map((r, i) => (
                <tr key={`${r.at}-${r.camera_id}-${i}`}>
                  <td style={{ whiteSpace: 'nowrap' }}>{new Date(r.at).toLocaleString()}</td>
                  {!cameraId && <td>{r.camera_name || r.camera_id}</td>}
                  <td>{r.state === 'at_preset' ? `📍 ${r.preset_name || r.preset_token}` : STATE_WORD[r.state] || r.state}</td>
                  <td>{causeText(r.cause, r.actor) || '—'}</td>
                  <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
                    {[r.pan, r.tilt].map(v => (v == null ? '—' : v.toFixed(3))).join(' / ')}
                  </td>
                  <td style={{ fontFamily: 'var(--mono)', fontSize: 11 }}
                      title={r.zoom == null ? undefined : `zoom ${r.zoom.toFixed(3)}`}>
                    {cameraId ? zoomFmt(r.zoom) : (r.zoom == null ? '—' : r.zoom.toFixed(3))}
                  </td>
                  {!cameraId && (
                    <td><a href="#" title="Recording at this moment" onClick={e => { e.preventDefault();
                      nav(`/playback?cam=${encodeURIComponent(r.camera_id || '')}&t=${Math.floor(new Date(r.at).getTime() / 1000)}`); }}>▶</a></td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      {data && <Pager page={page} size={PAGE} total={data.total} onPage={setPage} unit="changes" />}
    </div>
  );
}
