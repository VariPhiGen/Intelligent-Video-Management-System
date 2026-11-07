/**
 * PtzPage — every PTZ camera on one page (Cameras → PTZ): where each points now, who moved it, presets,
 * calibration and patrol state, searchable / filterable / sortable; below it the position history ACROSS
 * cameras ("what were the PTZ cameras doing between 02:00 and 02:30?") with filters, paging and CSV, and any
 * extension's site-wide PTZ panel (the patrols extension: the patrol run log).
 *
 * Patrol state is read from the patrols extension's /ptz/patrols/status when it is installed — a 404 just
 * leaves the column empty; the core does not depend on patrols existing.
 *
 * BULK CALIBRATION (2026-10-27): tick cameras (or "Not calibrated" / "No angles yet") → Calibrate. The server
 * queues them and calibrates ONE AT A TIME; the Calibration column shows each one live — queued (#n), which step,
 * done (levels, angles), failed (why) — with Stop per camera and Stop all. The page polls every 3 s while any is
 * queued or running, every 10 s otherwise.
 */
import { LoadingSkeleton } from '@/components/LoadingSkeleton';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiFetch } from '@/lib/api';
import { useToast } from '@/components/Toast';
import { ExtensionSlot } from '@/extensions';
import { causeText, positionLabel, ptzApi, type PtzOverviewRow } from '@/lib/ptz';
import { HistoryPanel } from '@/pages/ptz/HistoryPanel';
import { useAuth } from '@/lib/auth';
import { CamerasTabs } from './CamerasPage';

type Flag = 'off' | 'unknown' | 'uncal' | 'missing' | 'nopresets' | 'patrol_paused';
type Run = NonNullable<PtzOverviewRow['calibration_run']>;

/** Admin: how long movement history is kept — operator moves vs patrol steps — and how much room it takes now.
 *  Patrol steps are most of the rows (≈6 a minute per patrolling camera) and least of the value, hence shorter. */
function RetentionLine({ isAdmin }: { isAdmin: boolean }) {
  const toast = useToast();
  const [v, setV] = useState<{ operator_days: number; patrol_days: number } | null>(null);
  const [mb, setMb] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    ptzApi.settings().then(r => {
      setV(r.history);
      const b = Object.values(r.bytes || {}).filter((x): x is number => typeof x === 'number');
      setMb(b.length ? Math.round(b.reduce((a, x) => a + x, 0) / 1048576) : null);
    }).catch(() => {});
  }, []);
  if (!v) return null;
  const num = (k: 'operator_days' | 'patrol_days', lo: number, hi: number) => (
    <input type="number" min={lo} max={hi} value={v[k]} disabled={!isAdmin} style={{ width: 64 }} aria-label={k}
           onChange={e => setV({ ...v, [k]: Number(e.target.value) })} />
  );
  return (
    <div className="ptz-retention">
      History kept: operator moves {num('operator_days', 7, 365)} days · patrol steps {num('patrol_days', 1, 90)} days
      {mb != null && <span style={{ color: 'var(--dim)' }}> · using {mb} MB now</span>}
      {isAdmin && (
        <button className="btn-subtle btn-xs" disabled={busy} onClick={async () => {
          setBusy(true);
          try { const r = await ptzApi.saveSettings(v); setV(r.history); toast('History retention saved — applied at the next hourly clean-up'); }
          catch (e: any) { toast(e.message, 'err'); } finally { setBusy(false); }
        }}>Save</button>
      )}
    </div>
  );
}

/** The Calibration cell: what a camera's calibration is doing / did, in words. */
function CalibrationCell({ c, canManage, onStop }: { c: PtzOverviewRow; canManage: boolean; onStop: () => void }) {
  const run: Run | null | undefined = c.calibration_run;
  if (run?.state === 'queued') {
    return (<span className="ptz-cal queued">Queued{c.queue_position ? ` #${c.queue_position}` : ''}
      {run.note ? <span className="ptz-cal-why"> · {run.note}</span> : null}
      {canManage && <button className="btn-subtle btn-xs" onClick={onStop} title="Take it off the queue">✕</button>}</span>);
  }
  if (run?.state === 'running') {
    return (<span className="ptz-cal running"><span className="ptz-cal-dot" />Calibrating{run.step ? ` · ${run.step}` : ''}
      {canManage && <button className="btn-subtle btn-xs" onClick={onStop} title="Stop — the camera goes back where it was">Stop</button>}</span>);
  }
  const failed = run?.state === 'failed' ? (
    <div className="ptz-cal failed" title={run.error || ''}>✗ Last run failed{run.error ? `: ${run.error.slice(0, 90)}${run.error.length > 90 ? '…' : ''}` : ''}</div>
  ) : null;
  return (
    <>
      {c.calibrated
        ? <span className="ptz-cal done">✓ {c.calibration_levels || ''} zoom level{c.calibration_levels === 1 ? '' : 's'}
            {c.calibration_angles ? ' · angles' : <span className="ptz-cal-why"> · no angles — calibrate again</span>}</span>
        : <span style={{ color: 'var(--dim)' }}>not calibrated</span>}
      {failed}
    </>
  );
}
type SortKey = 'name' | 'state' | 'since' | 'presets';
interface PatrolRow { camera_id: string; enabled: boolean; status?: { state?: string; paused_by?: string | null } | null }

const FLAG_TEXT: Record<Flag, string> = {
  off: 'Off preset', unknown: 'Position unknown', uncal: 'Not calibrated', missing: 'Presets missing on camera',
  nopresets: 'No presets', patrol_paused: 'Patrol paused / error',
};

export function PtzPage() {
  const nav = useNavigate();
  const { me, isAdmin } = useAuth();
  const seesHistory = isAdmin || me?.permissions?.ptz_history === true;   // "PTZ movement history"
  const toast = useToast();
  const [rows, setRows] = useState<PtzOverviewRow[] | null>(null);
  const [patrols, setPatrols] = useState<Record<string, PatrolRow>>({});
  const [q, setQ] = useState('');
  const [flags, setFlags] = useState<Flag[]>([]);
  const [sort, setSort] = useState<SortKey>('name');
  const canManage = isAdmin || me?.permissions?.camera_manage === true;
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [tick, setTick] = useState(0);                       // bump = reload now
  const active = (rows || []).some(c => ['queued', 'running'].includes(c.calibration_run?.state || ''));

  useEffect(() => {
    let live = true;
    const load = () => {
      ptzApi.overview().then(r => { if (live) setRows(r.cameras || []); })
        .catch((e: any) => { if (live) { setRows([]); toast(e.message, 'err'); } });
      apiFetch<PatrolRow[]>('/ptz/patrols/status')
        .then(list => { if (live) setPatrols(Object.fromEntries((list || []).map(p => [p.camera_id, p]))); })
        .catch(() => {});
    };
    load();
    const t = setInterval(load, active ? 3000 : 10000);      // live while a calibration is queued or running
    return () => { live = false; clearInterval(t); };
  }, [toast, active, tick]);

  async function calibrate(ids: string[]) {
    if (!ids.length) return;
    const h = new Date().getHours();
    const dark = h < 6 || h >= 18;
    if (!window.confirm(`Calibrate ${ids.length} camera${ids.length === 1 ? '' : 's'}, one at a time (a few minutes each)? `
        + 'Each camera moves while it calibrates and goes back where it was afterwards.'
        + (dark ? '\n\nIt may be dark now — calibration needs detail in the picture and often fails at night.' : ''))) return;
    try {
      const r = await ptzApi.queueCalibration(ids);
      toast(`${r.added.length} queued${r.skipped.length ? ` · ${r.skipped.length} skipped (${r.skipped[0].why})` : ''}`);
      setPicked(new Set());
      setTick(t => t + 1);
    } catch (e: any) { toast(e.message, 'err'); }
  }
  async function stop(cameraId?: string) {
    try { await ptzApi.unqueueCalibration(cameraId, true); setTick(t => t + 1); }
    catch (e: any) { toast(e.message, 'err'); }
  }

  const shown = useMemo(() => {
    let list = [...(rows || [])];
    const needle = q.trim().toLowerCase();
    if (needle) list = list.filter(c => `${c.name} ${c.slug} ${c.position?.preset_name || ''}`.toLowerCase().includes(needle));
    for (const f of flags) {
      list = list.filter(c => {
        const p = patrols[c.id];
        return f === 'off' ? c.position?.state === 'off_preset'
          : f === 'unknown' ? !c.position || c.position.state === 'unknown'
          : f === 'uncal' ? !c.calibrated
          : f === 'missing' ? c.presets_missing > 0
          : f === 'nopresets' ? c.presets === 0
          : !!p && (!p.enabled || ['paused_by_operator', 'error'].includes(p.status?.state || ''));
      });
    }
    const by: Record<SortKey, (a: PtzOverviewRow, b: PtzOverviewRow) => number> = {
      name: (a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }),
      state: (a, b) => (a.position?.state || 'z').localeCompare(b.position?.state || 'z'),
      since: (a, b) => String(b.position?.since || '').localeCompare(String(a.position?.since || '')),
      presets: (a, b) => b.presets - a.presets,
    };
    return list.sort(by[sort]);
  }, [rows, q, flags, sort, patrols]);

  const toggle = (f: Flag) => setFlags(v => (v.includes(f) ? v.filter(x => x !== f) : [...v, f]));
  const cams = (rows || []).map(c => ({ id: c.id, name: c.name }));

  return (
    <div className="fade">
      <CamerasTabs active="ptz" />
      <div className="card" style={{ marginBottom: 12 }}>
        <div className="list-toolbar">
          <input type="search" value={q} onChange={e => setQ(e.target.value)} placeholder="Search PTZ cameras or presets…" />
          {(Object.keys(FLAG_TEXT) as Flag[]).map(f => (
            <span key={f} className={`chip-toggle${flags.includes(f) ? ' on' : ''}`} onClick={() => toggle(f)}>{FLAG_TEXT[f]}</span>
          ))}
          <div style={{ flex: 1 }} />
          {canManage && rows && rows.length > 0 && (
            <span className="ptz-cal-bar">
              <span style={{ color: 'var(--dim)' }}>Select</span>
              <button className="btn-subtle btn-xs" onClick={() => setPicked(new Set(shown.filter(c => !c.calibrated).map(c => c.id)))}>Not calibrated</button>
              <button className="btn-subtle btn-xs" onClick={() => setPicked(new Set(shown.filter(c => !c.calibration_angles).map(c => c.id)))}
                      title="Calibrated before angles were measured (or never)">No angles yet</button>
              <button className="btn-primary btn-sm" disabled={!picked.size} onClick={() => void calibrate([...picked])}>
                Calibrate{picked.size ? ` ${picked.size}` : ''}
              </button>
              {active && <button className="btn-subtle btn-sm" onClick={() => void stop()}
                                 title="Clear the queue and stop the camera calibrating now">Stop all</button>}
            </span>
          )}
          <select value={sort} onChange={e => setSort(e.target.value as SortKey)} aria-label="Sort">
            <option value="name">Name</option><option value="state">Where it points</option>
            <option value="since">Last moved</option><option value="presets">Most presets</option>
          </select>
        </div>
        {rows === null ? <LoadingSkeleton label="Loading PTZ cameras…" count={3} />
          : rows.length === 0 ? <div style={{ fontSize: 12, color: 'var(--dim)' }}>No PTZ cameras on this site (or their PTZ hasn’t been detected yet — open a camera’s PTZ tab once).</div>
          : (
            <table className="tbl" style={{ fontSize: 12.5 }}>
              <thead><tr>
                {canManage && <th style={{ width: 24 }}>
                  <input type="checkbox" aria-label="Select all shown" checked={shown.length > 0 && shown.every(c => picked.has(c.id))}
                         onChange={e => setPicked(e.target.checked ? new Set(shown.map(c => c.id)) : new Set())} />
                </th>}
                <th>Camera</th><th>Where it points</th><th>Since</th><th>Moved by</th><th>Presets</th>
                <th>Calibration</th><th>Patrol</th><th /></tr></thead>
              <tbody>
                {shown.map(c => {
                  const p = patrols[c.id];
                  const ps = !p ? '—' : !p.enabled ? 'Stopped' : ({ running: 'Patrolling', idle: 'Idle (outside schedule)',
                    paused_by_operator: `Paused${p.status?.paused_by ? ` by ${p.status.paused_by}` : ''}`, error: 'Error' } as Record<string, string>)[p.status?.state || ''] || 'Not reporting';
                  return (
                    <tr key={c.id}>
                      {canManage && <td><input type="checkbox" aria-label={`Select ${c.name}`} checked={picked.has(c.id)}
                        onChange={e => setPicked(s => { const n = new Set(s); if (e.target.checked) n.add(c.id); else n.delete(c.id); return n; })} /></td>}
                      <td><b>{c.name}</b>{c.health_status !== 'connected' && <span style={{ marginLeft: 6, fontSize: 11, color: 'var(--dim)' }}>offline</span>}</td>
                      <td>{positionLabel(c.position) || '—'}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>{c.position?.since ? new Date(c.position.since).toLocaleString() : '—'}</td>
                      <td>{causeText(c.position?.cause, c.position?.actor) || '—'}</td>
                      <td>{c.presets}{c.presets_missing ? <span style={{ color: 'var(--warn, #f59e0b)' }}> · {c.presets_missing} missing</span> : ''}</td>
                      <td><CalibrationCell c={c} canManage={canManage} onStop={() => void stop(c.id)} /></td>
                      <td>{ps}</td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button className="btn-subtle btn-xs" onClick={() => nav(`/cameras/config?cam=${c.id}&tab=ptz&ptz=control`)}>Control</button>{' '}
                        <button className="btn-subtle btn-xs" onClick={() => nav(`/live?cam=${c.id}`)}>Live</button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        {rows && rows.length > 0 && shown.length === 0 && <div style={{ fontSize: 12, color: 'var(--dim)' }}>No camera matches.</div>}
      </div>

      {seesHistory && (
        <div className="card" style={{ marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Position history — all PTZ cameras</div>
          <RetentionLine isAdmin={isAdmin} />
          <HistoryPanel cameras={cams} />
        </div>
      )}

      <ExtensionSlot name="ptz.page" cameras={cams} />
    </div>
  );
}
