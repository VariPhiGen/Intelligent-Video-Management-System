/**
 * TimelapseTab.tsx — Config → Time-lapse: switch a camera's time-lapse on and
 * set how it captures. OFF by default (migration 057), like motion and Smart
 * Search: nothing is captured until somebody with `timelapse_manage` says so.
 *
 * The estimate line is the point of the tab's layout: a time-lapse is a durable
 * record that outlives the camera's footage on purpose, so what it will cost
 * (stills a day, MB a day, the total over retention) is shown BEFORE saving and
 * recomputed by the backend on every change — the same arithmetic the server
 * uses, never a second copy of it here.
 */
import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import type { Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import {
  estimateLine, fmtBytes, presetOptions, statusText,
  type PtzPreset, type TimelapseCameraDetail, type TimelapseConfig, type TimelapseEstimate,
} from '@/pages/timelapse/timelapseBits';
import { TimelapseSettingsFields } from '@/pages/timelapse/TimelapseSettingsFields';

const label: CSSProperties = { fontSize: 11.5, color: 'var(--muted)' };
const row: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6 };

export function TimelapseTab({ camera, onSaved }: { camera: Camera; onSaved: () => void }) {
  const toast = useToast();
  const nav = useNavigate();
  const { me, isAdmin } = useAuth();
  const canManage = isAdmin || me?.permissions?.timelapse_manage === true;

  const [detail, setDetail] = useState<TimelapseCameraDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [cfg, setCfg] = useState<TimelapseConfig | null>(null);
  const [est, setEst] = useState<TimelapseEstimate | null>(null);
  const [saving, setSaving] = useState(false);
  const [presets, setPresets] = useState<PtzPreset[] | null>(null);
  const [presetErr, setPresetErr] = useState<string | null>(null);

  async function load() {
    try {
      const d = await apiFetch<TimelapseCameraDetail>(`/timelapse/cameras/${camera.id}`);
      setDetail(d); setEnabled(d.enabled); setCfg(d.config); setEst(d.estimate); setError(null);
    } catch (e: any) { setError(e.message); }
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [camera.id]);

  // Presets come from the camera itself (ONVIF), only for PTZ cameras and only for
  // someone who can change the setting — a viewer sees the saved value as text.
  useEffect(() => {
    if (!detail?.ptz_capable || !canManage) return;
    apiFetch<{ presets: PtzPreset[] }>(`/timelapse/cameras/${camera.id}/presets`)
      .then(r => { setPresets(r.presets); setPresetErr(null); })
      .catch((e: any) => { setPresets([]); setPresetErr(e.message); });
  }, [camera.id, detail?.ptz_capable, canManage]);

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

  const dirty = useMemo(() => !!detail && !!cfg &&
    (enabled !== detail.enabled || JSON.stringify(cfg) !== JSON.stringify(detail.config)), [detail, cfg, enabled]);

  if (error) return <div className="empty">{error}</div>;
  if (!detail || !cfg) return <div className="empty">Loading time-lapse settings…</div>;

  const set = <K extends keyof TimelapseConfig>(k: K, v: TimelapseConfig[K]) => setCfg({ ...cfg, [k]: v });
  const ro = !canManage;

  async function useDefaults() {
    if (!window.confirm('Put every setting of this camera back on the site defaults? (Its PTZ preset is kept.)')) return;
    try {
      const d = await apiFetch<TimelapseCameraDetail>(`/timelapse/cameras/${camera.id}/use-defaults`, { method: 'POST', body: '{}' });
      setDetail(d); setEnabled(d.enabled); setCfg(d.config); setEst(d.estimate);
      toast('This camera follows the default settings again');
      onSaved();
    } catch (e: any) { toast(e.message, 'err'); }
  }

  async function save() {
    setSaving(true);
    try {
      const d = await apiFetch<TimelapseCameraDetail>(`/timelapse/cameras/${camera.id}`, {
        method: 'PUT', body: JSON.stringify({ enabled, config: cfg }),
      });
      setDetail(d); setEnabled(d.enabled); setCfg(d.config); setEst(d.estimate);
      toast(d.enabled ? 'Time-lapse on for this camera' : 'Time-lapse settings saved');
      onSaved();
    } catch (e: any) { toast(e.message, 'err'); }
    setSaving(false);
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) 320px', gap: 'var(--s5)' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s4)' }}>
        {detail.paused && (
          <div className="panel" style={{ borderColor: 'var(--red)' }}>
            <div className="panel-body" style={{ fontSize: 13, lineHeight: 1.5 }}>
              <b style={{ color: 'var(--red)' }}>Paused automatically</b> on {new Date(detail.paused.at).toLocaleString()}: {detail.paused.reason}.
              {' '}Turn it back on from the <a href="#/timelapse?tab=defaults">Time-lapse page</a> once there is room.
            </div>
          </div>
        )}
        <TimelapseSettingsFields
          cfg={cfg} onChange={setCfg} ro={ro} defaults={detail.defaults} privacyMasks={detail.privacy_masks}
          lead={
            <div style={row}>
              <label style={label} htmlFor="tl-on">Time-lapse</label>
              <select id="tl-on" value={String(enabled)} disabled={ro} onChange={e => setEnabled(e.target.value === 'true')}>
                <option value="false">Off (default)</option>
                <option value="true">On: capture stills</option>
              </select>
            </div>
          }
          ptzSlot={detail.ptz_capable && (
            <div style={row}>
              <label style={label} htmlFor="tl-ptz">PTZ preset this time-lapse is framed on</label>
              <select id="tl-ptz" value={cfg.ptz_preset_only ?? ''} disabled={ro || (canManage && presets === null)}
                      onChange={e => set('ptz_preset_only', e.target.value || null)}>
                <option value="">Any position (don't check)</option>
                {presetOptions(presets, cfg.ptz_preset_only).map(o =>
                  <option key={o.value} value={o.value}>{o.label}</option>)}
              </select>
              <span style={label}>
                {presetErr
                  ? `Couldn't read the camera's presets: ${presetErr}`
                  : 'A still is kept only when the camera is at this preset — a patrol or an operator elsewhere is skipped. Cameras that cannot report their position are captured and marked unverified.'}
              </span>
            </div>
          )}
        />
        <div style={label}>
          {detail.changed && detail.changed.filter(k => k !== 'ptz_preset_only' && k !== 'ptz_tolerance').length
            ? <>Settings marked “Changed” are this camera's own; the rest follow the <a href="#/timelapse?tab=defaults">default settings</a>.</>
            : <>This camera follows the <a href="#/timelapse?tab=defaults">default settings</a>; anything you change here applies to this camera only.</>}
        </div>

        {canManage ? (
          <div style={{ display: 'flex', gap: 'var(--s3)', alignItems: 'center' }}>
            <button className="btn-primary" disabled={saving || !dirty} onClick={save}>{saving ? 'Saving…' : 'Save time-lapse'}</button>
            {dirty && <button className="btn-ghost" onClick={() => { setEnabled(detail.enabled); setCfg(detail.config); }}>Discard changes</button>}
            {!dirty && !!detail.changed?.some(k => k !== 'ptz_preset_only' && k !== 'ptz_tolerance') && (
              <button className="btn-ghost" onClick={useDefaults}>Use default settings</button>
            )}
          </div>
        ) : (
          <div style={label}>You can see these settings; changing them needs the “Manage time-lapse” permission.</div>
        )}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s4)' }}>
        <div className="panel">
          <div className="panel-head"><div className="panel-head-l"><div className="panel-title">What this will cost</div></div></div>
          <div className="panel-body" style={{ fontSize: 13, lineHeight: 1.5 }}>
            {est ? estimateLine(est, cfg.retention_days) : '…'}
            {!detail.object_store && (
              <div style={{ ...label, marginTop: 8 }}>No object store is configured, so stills are kept in the database.</div>
            )}
          </div>
        </div>
        <div className="panel">
          <div className="panel-head"><div className="panel-head-l"><div className="panel-title">Now</div></div></div>
          <div className="panel-body" style={{ fontSize: 13, display: 'flex', flexDirection: 'column', gap: 6 }}>
            <div>{detail.enabled ? 'Capturing' : 'Off'} · {detail.frames} stills · {fmtBytes(detail.bytes)}</div>
            <div style={label}>{statusText(detail.status)}{detail.status?.at ? ` · ${new Date(detail.status.at).toLocaleString()}` : ''}</div>
            {detail.last_captured_at && <div style={label}>Newest still {new Date(detail.last_captured_at).toLocaleString()}</div>}
            <button className="btn-ghost btn-sm" style={{ alignSelf: 'flex-start' }} onClick={() => nav(`/timelapse?cam=${camera.id}`)}>
              Open time-lapse →
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
