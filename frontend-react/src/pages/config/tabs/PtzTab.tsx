/**
 * PtzTab.tsx — the per-camera PTZ surface, in sub-tabs so each list has room for its own toolbar:
 *
 *   Control      the big live picture (click to centre, drag to zoom) with the pad, Home and capabilities
 *   Presets      VMS's record of the camera's presets — search / filter / sort / grid or list / reorder / paged
 *   History      where it pointed and who moved it — time range, filters, time-at-each-preset, "where was it
 *                at…?", paged, CSV
 *   Patrols      the patrols extension (editor + run log) through the camera.ptz slot
 *   Calibration  click-to-centre field-of-view measurement (services/ptz_calibrate)
 *
 * Driving the camera is `ptz_control`; saving presets / Home / calibrating is `camera_manage` — it changes where
 * anyone can send the camera from then on. The tab only exists on cameras that reported PTZ (ConfigPage).
 * The chosen sub-tab is in the URL (?ptz=), so a link can open History directly.
 *
 * ONE live picture serves every sub-tab that needs to see the camera (Control, Presets, Patrols, Calibration): it is
 * the same element in the same place in the tree, so switching sub-tabs re-lays it out instead of reconnecting the
 * stream. Control gives it the full width with the pad beside it; the others put it on the left (sticky) with a small
 * pad under it and the list / editor on the right — you aim and save a preset, or add the current view to a patrol,
 * without changing tabs. History hides it (the table needs the width) unless ?ptzlive=1. The camera header drops its
 * small still while the big picture is up (ConfigPage, ptzShowsLive) — one picture on screen, never two.
 */
import { useCallback, useEffect, useState } from 'react';
import { AuthImg } from '@/components/AuthImage';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '@/lib/auth';
import { useToast } from '@/components/Toast';
import { PtzPad, PtzSpeed } from '@/components/PtzPad';
import { LiveVideo, PtzPosBadge } from '@/pages/live/LiveVideo';
import { PtzClickLayer } from '@/pages/live/PtzControl';
import { HistoryPanel } from '@/pages/ptz/HistoryPanel';
import { PresetsPanel } from '@/pages/ptz/PresetsPanel';
import { AimPanel } from '@/pages/ptz/AimPanel';
import {
  HOME_TOKEN, anglesText, forgetCalibration, positionLabel, ptzApi, useZoomText, viewLabel,
  type PtzAngles, type PtzCalibration, type PtzCalibrationRun, type PtzCentreCheck, type PtzPosition, type PtzStatus,
  type PtzView,
} from '@/lib/ptz';
// Optional extensions add PTZ panels (the patrols extension: editor + run log). The core names none of them.
import { ExtensionSlot } from '@/extensions';
import type { Camera, PtzCapabilities, PtzPreset } from '@/lib/types';

const num = (v: number | null | undefined) => (v == null ? '—' : v.toFixed(3));
type Sub = 'control' | 'presets' | 'history' | 'patrols' | 'calibration';
const SUBS: { key: Sub; label: string }[] = [
  { key: 'control', label: 'Control' }, { key: 'presets', label: 'Presets' }, { key: 'history', label: 'History' },
  { key: 'patrols', label: 'Patrols' }, { key: 'calibration', label: 'Calibration' },
];

/** Whether the PTZ tab is showing its big live picture for these URL params — ConfigPage hides the header's small
 *  still while it is. History shows it only on "Show live" (?ptzlive=1). */
export function ptzShowsLive(sp: URLSearchParams): boolean {
  return sp.get('ptz') === 'history' ? sp.get('ptzlive') === '1' : true;
}

/** False once the browser tab has been hidden for `graceMs`: the live picture disconnects instead of streaming to
 *  nobody, and reconnects when the tab comes back. The grace keeps a quick alt-tab from costing a reconnect. */
function useDocVisible(graceMs = 30_000): boolean {
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    let t: ReturnType<typeof setTimeout> | undefined;
    const on = () => {
      clearTimeout(t);
      if (document.visibilityState === 'visible') setVisible(true);
      else t = setTimeout(() => setVisible(false), graceMs);
    };
    document.addEventListener('visibilitychange', on);
    return () => { clearTimeout(t); document.removeEventListener('visibilitychange', on); };
  }, [graceMs]);
  return visible;
}

function CapRow({ label, on, detail }: { label: string; on: boolean; detail?: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, fontSize: 12, padding: '3px 0' }}>
      <span style={{ color: 'var(--muted)' }}>{label}</span>
      <span style={{ color: on ? 'var(--text2)' : 'var(--dim)', fontFamily: 'var(--mono)', fontSize: 11.5 }}>
        {detail ?? (on ? 'yes' : 'no')}
      </span>
    </div>
  );
}

export function PtzTab({ camera, onRefresh }: { camera: Camera; onRefresh?: () => void }) {
  const toast = useToast();
  const { me, isAdmin } = useAuth();
  const canPtz = isAdmin || me?.permissions?.ptz_control === true;
  const canManage = isAdmin || me?.permissions?.camera_manage === true;
  // Who moved it, when: admin-only unless granted ("PTZ movement history"); the server refuses it too.
  const seesHistory = isAdmin || me?.permissions?.ptz_history === true;
  const subs = SUBS.filter(s => s.key !== 'history' || seesHistory);
  const [sp, setSp] = useSearchParams();
  const sub = (subs.some(s => s.key === sp.get('ptz')) ? sp.get('ptz') : 'control') as Sub;
  const setSub = (k: Sub) => { const n = new URLSearchParams(sp); n.set('ptz', k); setSp(n, { replace: true }); };

  const [caps, setCaps] = useState<PtzCapabilities>(camera.ptz!);
  const [speed, setSpeed] = useState(0.6);
  const [presets, setPresets] = useState<PtzPreset[] | null>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [status, setStatus] = useState<PtzStatus | null>(null);
  const [angles, setAngles] = useState<PtzAngles | null>(null);
  const [homeKnown, setHomeKnown] = useState(false);
  const zoomFmt = useZoomText(camera.id, camera.ptz);
  const [position, setPosition] = useState<PtzPosition | null>(null);
  const [view, setView] = useState<PtzView | null>(null);
  const vl = viewLabel(view, position);
  // The last preset the camera sat at — when it is then nudged off it, offer to update that preset to the new view.
  const [lastPreset, setLastPreset] = useState<{ token: string; name: string } | null>(null);
  useEffect(() => {
    if (position?.state === 'at_preset' && position.preset_token && position.preset_token !== HOME_TOKEN)
      setLastPreset({ token: position.preset_token, name: position.preset_name || position.preset_token });
  }, [position?.state, position?.preset_token, position?.preset_name]);
  useEffect(() => { setLastPreset(null); }, [camera.id]);
  const [busy, setBusy] = useState(false);
  // A preset hovered in the Presets list: its saved picture is shown over the live one ("saved" vs "now").
  const [peek, setPeek] = useState<PtzPreset | null>(null);
  const docVisible = useDocVisible();
  // From the sub-tab actually shown: a History link opened without the permission falls back to Control.
  const showLive = sub === 'history' ? ptzShowsLive(sp) : true;
  const wide = sub === 'control';
  const setLive = (on: boolean) => {
    const n = new URLSearchParams(sp);
    if (on) n.set('ptzlive', '1'); else n.delete('ptzlive');
    setSp(n, { replace: true });
  };
  useEffect(() => { if (sub !== 'presets') setPeek(null); }, [sub]);

  const loadPresets = useCallback(async () => {
    try {
      const r = await ptzApi.presets(camera.id) as { presets: PtzPreset[]; camera_error?: string | null };
      setPresets(r.presets || []);
      setCameraError(r.camera_error || null);
    } catch (e: any) {
      // A camera can be perfectly controllable and still refuse GetPresets.
      setPresets([]);
      toast(e.message, 'err');
    }
  }, [camera.id, toast]);
  useEffect(() => { void loadPresets(); }, [loadPresets]);

  // Position, polled while the tab is open (advisory: a camera that faults on GetStatus still drives fine).
  useEffect(() => {
    let alive = true;
    const poll = () => {
      ptzApi.status(camera.id).then(r => {
        if (!alive) return;
        setStatus(r.status);
        setAngles(r.angles ?? null);
        setHomeKnown(!!r.home_known);
      }).catch(() => { if (alive) setStatus(null); });
      ptzApi.position(camera.id).then(r => { if (alive) { setPosition(r.position); setView(r.view ?? null); } }).catch(() => {});
    };
    void poll();
    const t = setInterval(poll, 3000);
    return () => { alive = false; clearInterval(t); };
  }, [camera.id]);

  async function run(label: string, fn: () => Promise<unknown>, after?: () => void) {
    setBusy(true);
    try { await fn(); toast(label); after?.(); } catch (e: any) { toast(e.message, 'err'); } finally { setBusy(false); }
  }

  const quick = (presets || []).filter(p => !p.missing_on_camera)
    .sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0)).slice(0, 12);

  return (
    <>
      <div className="subtabs" role="tablist">
        {subs.map(s => (
          <button key={s.key} role="tab" aria-selected={sub === s.key} className={sub === s.key ? 'on' : ''}
                  onClick={() => setSub(s.key)}>
            {s.label}{s.key === 'presets' && presets ? ` (${presets.length})` : ''}
          </button>
        ))}
        <div style={{ flex: 1 }} />
        {position && <span style={{ fontSize: 12, alignSelf: 'center', color: 'var(--text2)' }}>{positionLabel(position)}</span>}
        {vl && <span title={vl.title} style={{ fontSize: 11.5, alignSelf: 'center', marginLeft: 8,
                                                color: vl.tone === 'ok' ? 'var(--ok, #16a34a)' : vl.tone === 'warn' ? 'var(--warn, #d97706)' : 'var(--text3, #8a94a6)' }}>
          {vl.text}</span>}
      </div>

      {/* Children keep fixed slots (null placeholders) so the live column never changes position in the tree —
          that is what keeps the stream connected across sub-tabs. */}
      {wide ? (
        <div className="d-hint" style={{ marginBottom: 12 }}>
          {canPtz
            ? 'Click the picture to centre on a spot, drag a box to zoom into it, right-click to zoom out, scroll to zoom — or hold a direction on the pad.'
            : 'Your role can see this camera’s PTZ configuration but not move it — PTZ control is a separate permission.'}
        </div>
      ) : null}
      {sub === 'history' ? (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
          <button className="btn-subtle btn-sm" onClick={() => setLive(!showLive)}>{showLive ? 'Hide live' : '▶ Show live'}</button>
        </div>
      ) : null}
      <div className={wide ? 'ptz-stage' : showLive ? 'ptz-stage ptz-split' : 'ptz-stage ptz-nolive'}>
        {showLive ? (
          <div className="ptz-live-col">
            <div className="ptz-live">
              {docVisible
                ? <LiveVideo slug={camera.slug} variant="focus" hasSub={!!camera.sub_track?.url_raw} />
                : <div className="ptz-paused">Live picture paused while this browser tab was in the background.</div>}
              <PtzClickLayer cam={camera} speed={speed} />
              {position && <div className="ptz-live-badge"><PtzPosBadge pos={position} /></div>}
              {peek?.has_thumbnail && (
                <div className="ptz-peek">
                  <AuthImg path={`/cameras/${camera.id}/ptz/presets/${encodeURIComponent(peek.token)}/thumbnail?v=${encodeURIComponent(peek.thumbnail_at || '')}`}
                           alt={`Saved view: ${peek.name || peek.token}`} />
                  <span>Saved: {peek.name || peek.token}</span>
                </div>
              )}
            </div>
            {!wide && canPtz ? (
              <div className="ptz-mini">
                <PtzPad cameraId={camera.id} caps={caps} speed={speed} onError={m => toast(m, 'err')} compact />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8, flex: 1, minWidth: 0 }}>
                  <PtzSpeed value={speed} onChange={setSpeed} />
                  {caps.home?.supported && (
                    <button className="btn-ghost btn-sm" style={{ alignSelf: 'flex-start' }} disabled={busy}
                            onClick={() => run('Returning to home position', () => ptzApi.gotoHome(camera.id, speed))}>
                      ⌂ Go to home
                    </button>
                  )}
                  <span style={{ fontSize: 11, color: 'var(--dim)' }}>Click the picture to centre · drag a box to zoom</span>
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
        <div className="ptz-side">
          {canManage && lastPreset && position?.state === 'off_preset' && (sub === 'control' || sub === 'presets') && (
            <div className="ptz-moved-from">
              <span>Moved from <b>{lastPreset.name}</b></span>
              <button className="btn-primary btn-sm" disabled={busy}
                      title="Overwrite that preset with where the camera is pointing now (patrols using it follow)"
                      onClick={() => {
                        if (!window.confirm(`Set “${lastPreset.name}” to where the camera is pointing now?`)) return;
                        void run(`“${lastPreset.name}” updated to this view`,
                                 () => ptzApi.savePreset(camera.id, lastPreset.name, lastPreset.token),
                                 () => { void loadPresets(); });
                      }}>
                Update “{lastPreset.name}” to this view
              </button>
              <button className="btn-subtle btn-xs" onClick={() => setLastPreset(null)} aria-label="Dismiss">✕</button>
            </div>
          )}
          {wide && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {canPtz
                ? <PtzPad cameraId={camera.id} caps={caps} speed={speed} onError={m => toast(m, 'err')} />
                : <div className="ptz-pad ptz-pad-off" style={{ width: 148, height: 148 }} aria-hidden />}
              {canPtz && <PtzSpeed value={speed} onChange={setSpeed} />}
              {caps.home?.supported && (
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  <button className="btn-ghost btn-sm" disabled={!canPtz || busy}
                          onClick={() => run('Returning to home position', () => ptzApi.gotoHome(camera.id, speed))}>
                    ⌂ Go to home
                  </button>
                  {canManage && (
                    <button className="btn-subtle btn-sm" disabled={busy} title="Make the camera's current position its home"
                            onClick={() => run('Home position set', () => ptzApi.setHome(camera.id))}>
                      Set current as home
                    </button>
                  )}
                </div>
              )}
              <AimPanel cameraId={camera.id} caps={caps} angles={angles} homeKnown={homeKnown} canPtz={canPtz}
                        canManage={canManage} speed={speed} onPresetSaved={() => { void loadPresets(); }} />
              {quick.length > 0 && (
                <div>
                  <div style={{ fontSize: 11.5, color: 'var(--dim)', marginBottom: 4 }}>Presets</div>
                  <div className="ptz-presets">
                    {quick.map(p => (
                      <button key={p.token} type="button" className="ptz-preset" disabled={!canPtz || busy}
                              onClick={() => run(`Moving to ${p.name || p.token}`, () => ptzApi.gotoPreset(camera.id, p.token, speed))}>
                        {p.is_home ? '⌂ ' : ''}{p.name || p.token}
                      </button>
                    ))}
                  </div>
                  {(presets || []).length > quick.length && (
                    <a href="#" style={{ fontSize: 11.5 }} onClick={e => { e.preventDefault(); setSub('presets'); }}>
                      All {(presets || []).length} presets →
                    </a>
                  )}
                </div>
              )}
              <div>
                <div style={{ fontSize: 12.5, fontWeight: 600, margin: '6px 0 4px' }}>Position</div>
                <CapRow label="Pan" on={status?.pan != null} detail={num(status?.pan)} />
                <CapRow label="Tilt" on={status?.tilt != null} detail={num(status?.tilt)} />
                <CapRow label="Zoom" on={status?.zoom != null}
                        detail={status?.zoom != null ? `${zoomFmt(status.zoom)} (${num(status.zoom)})` : num(status?.zoom)} />
                {angles && <div style={{ fontSize: 11.5, color: 'var(--text2)', marginTop: 4 }}>{anglesText(angles)}</div>}
                {status === null && <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>This camera does not report its position.</div>}
              </div>
            </div>
          )}

          {sub === 'presets' && (
            <PresetsPanel cameraId={camera.id} caps={caps} presets={presets} cameraError={cameraError} position={position}
                          canPtz={canPtz} canManage={canManage} speed={speed} reload={() => { void loadPresets(); }}
                          onPeek={setPeek} />
          )}

          {sub === 'history' && <HistoryPanel cameraId={camera.id} caps={caps} presets={presets} />}

          {sub === 'patrols' && <ExtensionSlot name="camera.ptz" camera={camera} />}

          {sub === 'calibration' && <CalibrationPanel camera={camera} caps={caps} canManage={canManage} />}
        </div>
      </div>
      {wide ? (
          <details style={{ marginTop: 6 }}>
            <summary style={{ cursor: 'pointer', fontSize: 12.5, fontWeight: 600 }}>Camera capabilities</summary>
            <div style={{ maxWidth: 360, marginTop: 8 }}>
              <CapRow label="Continuous pan/tilt" on={!!caps.continuous?.pan_tilt} />
              <CapRow label="Continuous zoom" on={!!caps.continuous?.zoom} />
              <CapRow label="Relative (step)" on={!!caps.relative?.pan_tilt} />
              <CapRow label="Absolute positioning" on={!!caps.absolute?.pan_tilt} />
              <CapRow label="Home position" on={!!caps.home?.supported} />
              <CapRow label="Max presets" on={caps.presets?.max != null}
                      detail={caps.presets?.max != null ? String(caps.presets.max) : 'not reported'} />
              {canManage && (
                <button className="btn-subtle btn-sm" style={{ marginTop: 10 }} disabled={busy}
                        title="Ask the camera again — use after a firmware upgrade or a hardware swap"
                        onClick={() => run('PTZ capabilities re-detected', async () => {
                          const r = await ptzApi.detect(camera.id);
                          setCaps(r.capabilities);
                          onRefresh?.();
                        }, loadPresets)}>
                  Re-detect capabilities
                </button>
              )}
              {caps.probed_at && (
                <div style={{ fontSize: 10.5, color: 'var(--dim)', marginTop: 6, fontFamily: 'var(--mono)' }}>
                  Probed {new Date(caps.probed_at).toLocaleString()}
                </div>
              )}
            </div>
          </details>
      ) : null}
    </>
  );
}

function CalibrationPanel({ camera, caps, canManage }: { camera: Camera; caps: PtzCapabilities; canManage: boolean }) {
  const toast = useToast();
  const [cal, setCal] = useState<PtzCalibration | null>(null);
  const [runState, setRunState] = useState<PtzCalibrationRun | null>(null);
  const [lastCheck, setLastCheck] = useState<PtzCentreCheck | null>(null);
  const load = useCallback(() => { forgetCalibration(camera.id); return ptzApi.calibration(camera.id)
    .then(r => { setCal(r.calibration); setRunState(r.run); setLastCheck(r.last_check ?? null); })
    .catch(() => {}); }, [camera.id]);
  const learnedClicks = Object.values(cal?.learned || {}).reduce((n, b) => n + (b.n || 0), 0);
  useEffect(() => { void load(); }, [load]);
  const running = runState?.state === 'running';
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => { void load(); }, 2000);
    return () => clearInterval(t);
  }, [running, load]);
  const canMove = !!(caps.absolute?.pan_tilt || caps.relative?.pan_tilt);
  return (
    <div className="ptz-section">
      <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 6 }}>Click-to-centre calibration</div>
      <div style={{ fontSize: 11.5, color: 'var(--muted)', marginBottom: 8, maxWidth: 720 }}>
        Clicking the live picture centres the camera on that spot, and dragging a box zooms into it. To land exactly,
        VMS measures this camera’s field of view once: small moves (each one checked by moving back) at up to eight
        zoom levels, then a few tilts toward the ground to measure its angles — a few minutes — comparing the
        pictures’ detail, and it returns to where it was. Point it at a detailed view with ground below it first (not
        the sky or a blank wall). After that, every click is checked: if the spot didn’t land in the middle the
        camera corrects once, and remembers it for that zoom.
      </div>
      {runState?.state === 'queued' ? (
        <div style={{ fontSize: 12 }}>Queued for calibration (Cameras → PTZ) — it starts when the cameras ahead of it are done.</div>
      ) : running ? (
        <div style={{ fontSize: 12 }}>Calibrating… {runState?.step || ''} — the camera is moving; it returns to its position when done.</div>
      ) : cal ? (
        <div style={{ fontSize: 12 }}>
          ✓ Calibrated {new Date(cal.measured_at).toLocaleString()}{cal.by ? ` by ${cal.by}` : ''} · {cal.points.length} zoom levels
          {cal.points.length > 1 && ` · ${(Math.abs(cal.points[0].pan_per_width) / Math.abs(cal.points[cal.points.length - 1].pan_per_width)).toFixed(0)}× optical range measured`}
          <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 3 }}>
            {cal.geometry
              ? `Angles measured: ${Math.round(cal.geometry.kt)}° per tilt unit, level at tilt ${cal.geometry.t0.toFixed(3)}`
              : 'Angles not measured (camera stayed near level, or calibrated before this existed) — straight-line centring'}
            {learnedClicks > 0 && ` · fine-tuned from ${learnedClicks} click${learnedClicks === 1 ? '' : 's'}`}
          </div>
          {lastCheck && (
            <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 2 }}>
              Last click {new Date(lastCheck.at).toLocaleTimeString()}:{' '}
              {!lastCheck.checked ? `not checked — ${lastCheck.why || 'no picture'}`
                : lastCheck.corrected ? 'missed a little, corrected'
                : 'landed in the middle'}
            </div>
          )}
        </div>
      ) : (
        <div style={{ fontSize: 12, color: 'var(--dim)' }}>Not calibrated — clicks centre approximately.</div>
      )}
      {runState?.state === 'failed' && runState.error && (
        <div style={{ fontSize: 11.5, color: 'var(--warn, #f59e0b)', marginTop: 4 }}>Last calibration failed: {runState.error}</div>
      )}
      {(runState?.notes?.length || cal?.notes?.length) ? (
        <details style={{ marginTop: 8, fontSize: 11.5 }}>
          <summary style={{ cursor: 'pointer', color: 'var(--muted)' }}>What the calibration saw</summary>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 11, color: 'var(--text2)', margin: '6px 0 0' }}>
            {(runState?.notes?.length ? runState.notes : cal?.notes || []).join('\n')}
          </pre>
        </details>
      ) : null}
      {canManage && canMove && !running && (
        <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
          <button className="btn-primary btn-sm"
                  onClick={() => {
                    if (!window.confirm('Calibration moves this camera for a few minutes, then puts it back. Start now?')) return;
                    ptzApi.calibrate(camera.id).then(() => { toast('Calibration started'); void load(); })
                      .catch((e: any) => toast(e.message, 'err'));
                  }}>
            {cal ? 'Calibrate again' : 'Calibrate'}
          </button>
          {cal && (
            <button className="btn-subtle btn-sm"
                    onClick={() => ptzApi.clearCalibration(camera.id).then(() => { toast('Calibration cleared'); void load(); })
                      .catch((e: any) => toast(e.message, 'err'))}>
              Clear
            </button>
          )}
        </div>
      )}
    </div>
  );
}

