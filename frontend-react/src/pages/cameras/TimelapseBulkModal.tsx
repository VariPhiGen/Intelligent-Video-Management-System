/**
 * TimelapseBulkModal.tsx — the Inventory's "Time-lapse on / off" for the ticked
 * cameras, in one POST /timelapse/bulk.
 *
 * Not a second settings page. The default path sends NO settings: each camera
 * keeps its own (a never-configured one gets the same defaults its Time-lapse
 * tab would show), which is what "switch these on" means to someone selecting a
 * whole site. "Set capture settings" exposes only the four fields a site-wide
 * choice makes sense for; the backend overlays exactly those on each camera,
 * so a PTZ camera's preset or a lengthened retention elsewhere survives.
 *
 * The defaults the form starts from come from GET /timelapse/defaults — the
 * backend's TimelapseConfig — never a copy kept here that could drift.
 */
import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import {
  INTERVALS, WEEKDAYS, bulkAttention, bulkSettingsFrom, bulkSummary, diskLine, estimateLine, fitsBudget, fmtBytes, fmtMb,
  type BulkResult, type BulkSettings, type TimelapseDefaults, type TimelapseEstimate,
} from '@/pages/timelapse/timelapseBits';

const label: CSSProperties = { fontSize: 11.5, color: 'var(--muted)' };
const row: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6 };

export type BulkTimelapseMode = 'on' | 'off' | null;

export function TimelapseBulkModal({ mode, ids, onClose, onDone }: {
  mode: BulkTimelapseMode;
  /** Ids of the ticked rows. */
  ids: string[];
  onClose: () => void;
  /** Called after a request that changed something, so the table can refresh. */
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<BulkResult | null>(null);
  const [custom, setCustom] = useState(false);
  const [settings, setSettings] = useState<BulkSettings | null>(null);
  const [bulkMax, setBulkMax] = useState(500);
  const [est, setEst] = useState<TimelapseEstimate | null>(null);
  const [site, setSite] = useState<TimelapseDefaults | null>(null);

  // Fresh state every time the dialog opens: a result from the last run must
  // never sit above a new selection.
  useEffect(() => {
    if (!mode) return;
    setBusy(false); setErr(null); setResult(null); setCustom(false); setEst(null);
    apiFetch<TimelapseDefaults>('/timelapse/defaults')
      .then(d => { setSite(d); setSettings(bulkSettingsFrom(d.config)); setBulkMax(d.bulk_max); setEst(d.estimate); })
      .catch(() => setSettings(null));
  }, [mode]);

  // The estimate is per camera and computed by the server (same arithmetic the
  // tab uses); debounced like the tab.
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!custom || !settings) return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      apiFetch<TimelapseEstimate>('/timelapse/estimate', { method: 'POST', body: JSON.stringify({ config: settings }) })
        .then(setEst).catch(() => { /* an invalid draft keeps the last good estimate */ });
    }, 300);
    return () => window.clearTimeout(timer.current);
  }, [custom, settings]);

  if (!mode) return null;
  const on = mode === 'on';
  const n = ids.length;
  const plural = n === 1 ? '' : 's';
  const tooMany = n > bulkMax;

  async function run() {
    setBusy(true); setErr(null);
    try {
      const body: Record<string, unknown> = { camera_ids: ids, enabled: on };
      if (on && custom && settings) body.config = settings;
      const r = await apiFetch<BulkResult>('/timelapse/bulk', { method: 'POST', body: JSON.stringify(body) });
      setResult(r);
      if (r.changed) onDone();
    } catch (e: any) { setErr(e.message); }
    setBusy(false);
  }

  const close = () => { if (!busy) onClose(); };

  if (result) {
    const attention = bulkAttention(result);
    return (
      <Modal open title={on ? 'Time-lapse switched on' : 'Time-lapse switched off'} width={560} onClose={close}>
        <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 10 }}>{bulkSummary(result, on)}</div>
        {attention.length > 0 && (
          <div className="table-wrap" style={{ maxHeight: 320, overflow: 'auto', marginBottom: 12 }}>
            <table>
              <thead><tr><th>Camera</th><th>Outcome</th></tr></thead>
              <tbody>
                {attention.map(x => (
                  <tr key={x.camera_id}>
                    <td style={{ fontWeight: 600 }}>{x.name || x.camera_id}</td>
                    <td style={{ fontSize: 12 }}>
                      {x.ok
                        ? <>{x.result?.replace('_', ' ')}{(x.notes || []).map(t => <div key={t} style={label}>{t}</div>)}</>
                        : <span style={{ color: 'var(--red)' }}>{x.error}</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {on && result.changed > 0 && (
          <div style={{ ...label, marginBottom: 12 }}>
            The first stills arrive within one capture interval, inside each camera's schedule — see the Time-lapse page.
          </div>
        )}
        <div className="form-actions">
          <button className="btn-primary" onClick={onClose}>Done</button>
        </div>
      </Modal>
    );
  }

  const s = settings;
  const rule = s?.schedule?.rules[0];
  const simple = !s?.schedule || (s.schedule.mode === 'weekly' && s.schedule.rules.length === 1);
  const set = <K extends keyof BulkSettings>(k: K, v: BulkSettings[K]) => s && setSettings({ ...s, [k]: v });
  const setRule = (patch: Partial<{ days: number[]; start: string; end: string }>) =>
    set('schedule', { mode: 'weekly', rules: [{ days: rule?.days ?? [0, 1, 2, 3, 4, 5], start: rule?.start ?? '07:00',
                                               end: rule?.end ?? '19:00', ...patch }] });

  return (
    <Modal open title={on ? `Time-lapse on for ${n} camera${plural}` : `Time-lapse off for ${n} camera${plural}`}
           width={on ? 560 : 460} onClose={close}>
      {on ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s3)', fontSize: 13, lineHeight: 1.55 }}>
          <div>
            Each camera captures stills on its own schedule. Cameras that already have time-lapse settings keep
            them; the others get the defaults. Offline cameras are switched on too — capture starts when they are back.
          </div>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="checkbox" style={{ width: 'auto' }} checked={custom} disabled={!s}
                   onChange={e => setCustom(e.target.checked)} />
            Set capture settings for all {n} camera{plural}
          </label>
          {custom && s && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 'var(--s3)' }}>
              <div style={row}>
                <label style={label} htmlFor="btl-int">Capture</label>
                <select id="btl-int" value={s.interval_s} onChange={e => set('interval_s', Number(e.target.value))}>
                  {INTERVALS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </div>
              <div style={row}>
                <label style={label} htmlFor="btl-res">Resolution</label>
                <select id="btl-res" value={s.resolution}
                        onChange={e => set('resolution', e.target.value as BulkSettings['resolution'])}>
                  <option value="1080p">1080p (default)</option>
                  <option value="720p">720p: smaller files</option>
                  <option value="original">Camera's own resolution</option>
                </select>
              </div>
              <div style={row}>
                <label style={label} htmlFor="btl-ret">Keep stills for (days)</label>
                <input id="btl-ret" type="number" min={1} max={3650} value={s.retention_days}
                       onChange={e => set('retention_days', Number(e.target.value))} />
              </div>
              <div style={row}>
                <label style={label} htmlFor="btl-when">When</label>
                <select id="btl-when" value={s.schedule ? (simple ? 'hours' : 'custom') : 'always'}
                        onChange={e => set('schedule', e.target.value === 'always' ? null
                          : { mode: 'weekly', rules: [{ days: [0, 1, 2, 3, 4, 5], start: '07:00', end: '19:00' }] })}>
                  <option value="hours">Set hours and days</option>
                  <option value="always">Around the clock</option>
                </select>
              </div>
              {s.schedule && rule && (
                <div style={{ gridColumn: '1 / -1', display: 'flex', flexWrap: 'wrap', gap: 'var(--s3)', alignItems: 'center' }}>
                  <input type="time" value={rule.start} aria-label="From" onChange={e => setRule({ start: e.target.value })} />
                  <span style={label}>to</span>
                  <input type="time" value={rule.end === '24:00' ? '23:59' : rule.end} aria-label="To"
                         onChange={e => setRule({ end: e.target.value })} />
                  <span style={{ display: 'flex', gap: 4 }}>
                    {WEEKDAYS.map((d, i) => (
                      <label key={d} style={{ display: 'flex', alignItems: 'center', gap: 3, fontSize: 12 }}>
                        <input type="checkbox" style={{ width: 'auto' }} checked={rule.days.includes(i)}
                               onChange={e => setRule({ days: e.target.checked ? [...new Set([...rule.days, i])].sort()
                                                                               : rule.days.filter(x => x !== i) })} />
                        {d}
                      </label>
                    ))}
                  </span>
                </div>
              )}
              <div style={{ ...label, gridColumn: '1 / -1' }}>
                These replace only these four settings on each camera, and become those cameras' own settings
                (listed under Time-lapse → Changed per camera). Leave this unticked to use the default settings.
                A PTZ camera's preset and the skip rules stay as set on its own Time-lapse tab.
              </div>
            </div>
          )}
          {est && s && (
            <div style={label}>Per camera: {estimateLine(est, custom ? s.retention_days : (site?.config.retention_days ?? 90))}</div>
          )}
          {on && site && (() => {
            // Worst case for the selection against the room the disk guard leaves, before anyone presses the button.
            const fit = est ? fitsBudget(est.mb_total, n, site.disk) : null;
            return (
              <div style={{ ...label, marginTop: 6 }}>
                {diskLine(site.disk)}
                {site.disk.over && <div style={{ color: 'var(--red)', marginTop: 4 }}>
                  Time-lapse can't be switched on right now: {site.disk.reason}.</div>}
                {!site.disk.over && fit && !fit.fits && <div style={{ color: 'var(--yellow)', marginTop: 4 }}>
                  {n} camera{plural} × up to {fmtMb(est!.mb_total)} = {fmtBytes(fit.needBytes)}, more than the {fmtBytes(fit.leftBytes)} left
                  for time-lapse: it will switch itself off before they reach their size cap. Choose fewer stills, 720p or
                  a shorter keep.</div>}
              </div>
            );
          })()}
        </div>
      ) : (
        <div style={{ fontSize: 13, lineHeight: 1.6 }}>
          Stop capturing stills on <b>{n}</b> camera{plural}? Stills and videos already made are kept until their
          retention runs out, and each camera keeps its settings for when time-lapse is switched on again.
        </div>
      )}
      {tooMany && <div style={{ color: 'var(--red)', fontSize: 12.5, marginTop: 10 }}>
        At most {bulkMax} cameras at a time — narrow the selection.</div>}
      {err && <div style={{ color: 'var(--red)', fontSize: 12.5, marginTop: 10 }}>{err}</div>}
      <div className="form-actions">
        <button className={on ? 'btn-primary' : 'btn-danger'} onClick={run} disabled={busy || tooMany || !n || (on && !!site?.disk.over)}>
          {busy ? 'Working…' : on ? `Turn on for ${n}` : `Turn off for ${n}`}
        </button>
        <button className="btn-ghost" onClick={close} disabled={busy}>Cancel</button>
      </div>
    </Modal>
  );
}
