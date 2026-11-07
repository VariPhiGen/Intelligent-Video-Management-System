/**
 * TimelapseSettingsViews.tsx — the Time-lapse page's settings tabs and its pause banner.
 *
 *   DefaultsView  the site's default settings (every camera follows them unless
 *                 it changes a setting itself) and the disk guard's limits
 *
 * Permissions mirror the server (routers/timelapse.py): timelapse_view reads all
 * of it; timelapse_manage changes the defaults, resets cameras and resumes after
 * a pause; the disk limits are admin-only, because they decide how close stills
 * may push the recorder to deleting footage.
 *   ChangedView   only the cameras whose settings differ from the defaults,
 *                 setting by setting, each with "Use defaults" / "Edit"
 *   PauseBanner   the disk guard's remark, with "Turn back on" — shown on every tab
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiFetch } from '@/lib/api';
import { useToast } from '@/components/Toast';
import { TimelapseSettingsFields } from './TimelapseSettingsFields';
import {
  SETTING_LABEL, diskLine, estimateLine, fitsBudget, fmtBytes, fmtMb, settingText,
  type ChangedCamera, type TimelapseConfig, type TimelapseDefaults, type TimelapseEstimate, type TimelapseGuard,
  type TimelapsePause,
} from './timelapseBits';

const muted: CSSProperties = { fontSize: 11.5, color: 'var(--muted)' };

export function PauseBanner({ paused, canManage, onChanged }: {
  paused: TimelapsePause | null | undefined; canManage: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!paused) return null;
  async function act(path: string, ok: (r: any) => string) {
    setBusy(true);
    try { const r = await apiFetch<any>(path, { method: 'POST', body: '{}' }); toast(ok(r)); onChanged(); }
    catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }
  return (
    <div className="panel" style={{ borderColor: 'var(--red)', marginBottom: 'var(--s4)' }}>
      <div className="panel-body" style={{ display: 'flex', gap: 'var(--s3)', alignItems: 'center', flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 260, fontSize: 13, lineHeight: 1.5 }}>
          <b style={{ color: 'var(--red)' }}>Time-lapse was switched off on {paused.camera_ids.length} camera{paused.camera_ids.length === 1 ? '' : 's'}</b>
          {' '}on {new Date(paused.at).toLocaleString()}: {paused.reason}.
          <div style={muted}>No still was deleted. Free some space, or lower the settings (fewer stills, 720p, a smaller size cap), then turn it back on.</div>
        </div>
        {canManage && <>
          <button className="btn-primary btn-sm" disabled={busy}
                  onClick={() => act('/timelapse/guard/resume', r => `Time-lapse back on for ${r.resumed} camera${r.resumed === 1 ? '' : 's'}`)}>
            Turn back on
          </button>
          <button className="btn-ghost btn-sm" disabled={busy} title="Hide this note and leave time-lapse off"
                  onClick={() => act('/timelapse/guard/dismiss', () => 'Note cleared; time-lapse stays off')}>
            Dismiss
          </button>
        </>}
      </div>
    </div>
  );
}

export function DefaultsView({ canManage, isAdmin, onChanged }: { canManage: boolean; isAdmin: boolean; onChanged: () => void }) {
  const toast = useToast();
  const [d, setD] = useState<TimelapseDefaults | null>(null);
  const [cfg, setCfg] = useState<TimelapseConfig | null>(null);
  const [guard, setGuard] = useState<TimelapseGuard | null>(null);
  const [est, setEst] = useState<TimelapseEstimate | null>(null);
  const [saving, setSaving] = useState(false);

  const load = () => apiFetch<TimelapseDefaults>('/timelapse/defaults')
    .then(r => { setD(r); setCfg(r.config); setGuard(r.guard); setEst(r.estimate); })
    .catch((e: any) => toast(e.message, 'err'));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, []);

  // Re-estimate on every edit, debounced: the server owns the arithmetic.
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!cfg) return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      apiFetch<TimelapseEstimate>('/timelapse/estimate', { method: 'POST', body: JSON.stringify({ config: cfg }) })
        .then(setEst).catch(() => { /* an invalid draft keeps the last good estimate */ });
    }, 300);
    return () => window.clearTimeout(timer.current);
  }, [cfg]);

  const dirty = useMemo(() => !!d && !!cfg && !!guard &&
    (JSON.stringify(cfg) !== JSON.stringify(d.config) || JSON.stringify(guard) !== JSON.stringify(d.guard)), [d, cfg, guard]);

  if (!d || !cfg || !guard) return <div className="empty">Loading default settings…</div>;
  const ro = !canManage;
  const guardRo = !isAdmin;
  const n = d.enabled_cameras;
  const fit = est ? fitsBudget(est.mb_total, Math.max(1, n), d.disk) : null;

  async function save() {
    setSaving(true);
    try {
      // Without the per-camera-only keys: a PTZ preset isn't a site default.
      const defaults = Object.fromEntries(Object.entries(cfg!).filter(([k]) => !d!.site_only_excludes.includes(k)));
      // The limits go only from an admin: the server refuses a change from anyone else.
      const body = isAdmin ? { defaults, guard } : { defaults };
      const r = await apiFetch<TimelapseDefaults>('/timelapse/defaults', { method: 'PUT', body: JSON.stringify(body) });
      setD(r); setCfg(r.config); setGuard(r.guard); setEst(r.estimate);
      toast('Default settings saved — every camera that follows them uses them from its next still');
      onChanged();
    } catch (e: any) { toast(e.message, 'err'); }
    setSaving(false);
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: 'var(--s5)' }} className="tl-split">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s4)', minWidth: 0 }}>
        <div style={muted}>
          Every camera uses these settings unless it changes one on its own Time-lapse tab (those are listed under
          “Changed per camera”). A PTZ camera's preset is always set per camera.
        </div>
        <TimelapseSettingsFields cfg={cfg} onChange={setCfg} ro={ro} />

        <div className="panel">
          <div className="panel-head"><div className="panel-head-l"><div>
            <div className="panel-title">Disk protection</div>
            <div className="panel-sub">
              Stills share the server disk with recordings. When the disk gets too full the recorder deletes the oldest
              footage, so time-lapse switches itself off first and says why.
            </div>
          </div></div></div>
          <div className="panel-body" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'var(--s4)' }}>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={muted}>Switch off when free disk is below (%)</span>
              <input type="number" min={5} max={50} value={guard.floor_free_pct} disabled={guardRo}
                     onChange={e => setGuard({ ...guard, floor_free_pct: Number(e.target.value) })} />
            </label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={muted}>Time-lapse may use at most (% of the disk)</span>
              <input type="number" min={1} max={90} value={guard.budget_pct} disabled={guardRo}
                     onChange={e => setGuard({ ...guard, budget_pct: Number(e.target.value) })} />
            </label>
            <div style={{ gridColumn: '1 / -1', ...muted }}>
              {diskLine(d.disk)}
              {guardRo && <div>Only an administrator can change these limits.</div>}
            </div>
          </div>
        </div>

        {canManage ? (
          <div style={{ display: 'flex', gap: 'var(--s3)', alignItems: 'center' }}>
            <button className="btn-primary" disabled={saving || !dirty} onClick={save}>{saving ? 'Saving…' : 'Save default settings'}</button>
            {dirty && <button className="btn-ghost" onClick={() => { setCfg(d.config); setGuard(d.guard); }}>Discard changes</button>}
            <button className="btn-subtle btn-sm" style={{ marginLeft: 'auto' }} disabled={ro}
                    title="Fill the form with the settings VMS ships with (save to apply)"
                    onClick={() => setCfg({ ...d.builtin })}>Load factory settings</button>
          </div>
        ) : (
          <div style={muted}>You can see these settings; changing them needs the “Manage time-lapse” permission.</div>
        )}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s4)' }}>
        <div className="panel">
          <div className="panel-head"><div className="panel-head-l"><div className="panel-title">What this will cost</div></div></div>
          <div className="panel-body" style={{ fontSize: 13, lineHeight: 1.5, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <div><b>Per camera:</b> {est ? estimateLine(est, cfg.retention_days) : '…'}</div>
            {est && fit && (
              <div>
                <b>{n} camera{n === 1 ? '' : 's'} on now:</b> up to {fmtMb(est.mb_total * n)}
                {d.disk.total_bytes ? <> · room left for time-lapse {fmtBytes(fit.leftBytes)}</> : null}
                {!fit.fits && (
                  <div style={{ color: 'var(--yellow)', marginTop: 4 }}>
                    More than the room left: time-lapse will switch itself off before the cameras reach their size cap.
                    Lower the size cap, the resolution or how often stills are taken.
                  </div>
                )}
              </div>
            )}
            <div style={muted}>Cameras that changed a setting themselves use their own value for it.</div>
          </div>
        </div>
      </div>
    </div>
  );
}

export function ChangedView({ canManage, canOpenCamera, onChanged }: {
  canManage: boolean; canOpenCamera: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const nav = useNavigate();
  const [rows, setRows] = useState<ChangedCamera[] | null>(null);
  const load = () => apiFetch<{ cameras: ChangedCamera[] }>('/timelapse/changed')
    .then(r => setRows(r.cameras)).catch((e: any) => { toast(e.message, 'err'); setRows([]); });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, []);

  async function useDefaults(c: ChangedCamera, keys?: string[]) {
    try {
      await apiFetch(`/timelapse/cameras/${c.id}/use-defaults`, { method: 'POST', body: JSON.stringify(keys ? { keys } : {}) });
      toast(keys ? `${c.name}: ${SETTING_LABEL[keys[0] as keyof TimelapseConfig] || keys[0]} follows the default again`
                 : `${c.name} follows the default settings again`);
      load(); onChanged();
    } catch (e: any) { toast(e.message, 'err'); }
  }

  if (rows === null) return <div className="empty">Loading…</div>;
  if (!rows.length) {
    return <div className="empty">Every camera follows the default settings. A camera appears here once a setting is changed on its own Time-lapse tab.</div>;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s3)' }}>
      <div style={muted}>These cameras use their own value for the settings listed; everything else follows the default settings.</div>
      {rows.map(c => {
        const keys = Object.keys(c.changes);
        const resettable = keys.filter(k => k !== 'ptz_preset_only' && k !== 'ptz_tolerance');
        return (
          <div key={c.id} className="panel">
            <div className="panel-head">
              <div className="panel-head-l"><div>
                <div className="panel-title">{c.name}</div>
                <div className="panel-sub">{c.enabled ? 'Time-lapse on' : 'Time-lapse off'} · {keys.length} setting{keys.length === 1 ? '' : 's'} changed</div>
              </div></div>
              <div style={{ display: 'flex', gap: 'var(--s2)' }}>
                {canOpenCamera && (
                  <button className="btn-ghost btn-sm" onClick={() => nav(`/cameras/config?cam=${c.id}&tab=timelapse`)}>Edit</button>
                )}
                {canManage && resettable.length > 0 && (
                  <button className="btn-ghost btn-sm" onClick={() => useDefaults(c)}>Use defaults</button>
                )}
              </div>
            </div>
            <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {keys.map(k => (
                <div key={k} style={{ display: 'flex', gap: 'var(--s3)', alignItems: 'baseline', flexWrap: 'wrap', fontSize: 13 }}>
                  <span style={{ minWidth: 170, color: 'var(--muted)' }}>{SETTING_LABEL[k as keyof TimelapseConfig] || k}</span>
                  <b>{settingText(k, c.changes[k].value)}</b>
                  <span style={muted}>
                    {k === 'ptz_preset_only' || k === 'ptz_tolerance' ? 'set per camera' : `default: ${settingText(k, c.changes[k].default)}`}
                  </span>
                  {canManage && resettable.includes(k) && resettable.length > 1 && (
                    <button className="btn-subtle btn-sm" style={{ marginLeft: 'auto' }} onClick={() => useDefaults(c, [k])}>Use default</button>
                  )}
                </div>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
