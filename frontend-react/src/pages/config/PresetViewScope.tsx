/**
 * PresetViewScope.tsx — "which view am I configuring?" above Zones & Analytics and AI Config on a PTZ camera.
 *
 * Each preset is a fixed-camera view with its own zones, tripwires and rules (backend services/ptz_views). Pick the
 * whole camera or a preset; the tabs below then edit THAT config, drawn on the preset's own reference picture —
 * not on wherever the camera happens to point right now. Once any preset has a rule, the camera's AI follows its
 * presets: a preset's rules run only while the camera is confirmed there, and nothing runs between presets.
 *
 * When a preset's VIEW changed under its zones (re-saved, or re-stored in the camera's own settings — backend
 * services/ptz_view_changes) a banner says what happened: zones moved with the view (still running; accept or undo),
 * or rules paused until someone checks them here on the new picture (saving the zones, or "Zones are right").
 * Deleted presets keep their zones + rules: "Deleted presets" copies them onto the preset being viewed.
 *
 * Fixed cameras: no bar, the tabs edit the camera as before.
 */
import { useEffect, useState, type ReactNode } from 'react';
import type { AnalyticsConfig, Camera, PtzArchivedView, PtzPreset } from '@/lib/types';
import { AuthImg } from '@/components/AuthImage';
import { ptzApi } from '@/lib/ptz';
import { useToast } from '@/components/Toast';
import { aiText, cameraTarget, followsPresets, presetTarget, type AnalyticsTarget } from './analytics/analyticsTarget';

const when = (s?: string | null) => (s ? new Date(s).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '');

export function PresetViewScope({ camera, view, onView, onSaved, children }: {
  camera: Camera;
  /** "camera" or a preset token (the ?view= of the page). */
  view: string;
  onView: (v: string) => void;
  onSaved: () => void;
  children: (cam: Camera, target: AnalyticsTarget, key: string, onSaved: () => void) => ReactNode;
}) {
  const toast = useToast();
  const ptz = !!camera.ptz_capable;
  const [presets, setPresets] = useState<PtzPreset[] | null>(null);
  const [cfg, setCfg] = useState<AnalyticsConfig | null>(null);
  const [night, setNight] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [archived, setArchived] = useState<PtzArchivedView[]>([]);
  const [showBefore, setShowBefore] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  useEffect(() => {
    if (!ptz) return;
    let alive = true;
    ptzApi.presets(camera.id).then(r => { if (alive) setPresets(r.presets.filter(p => !p.missing_on_camera)); })
      .catch(() => { if (alive) setPresets([]); });
    ptzApi.archivedViews(camera.id).then(r => { if (alive) setArchived(r.archived); }).catch(() => { if (alive) setArchived([]); });
    return () => { alive = false; };
  }, [camera.id, ptz, reload]);

  const preset = ptz && view !== 'camera' ? (presets || []).find(p => p.token === view) : undefined;
  const target = preset ? presetTarget(camera, preset, night) : cameraTarget(camera);

  useEffect(() => {
    setCfg(null);
    if (!preset) return;
    let alive = true;
    presetTarget(camera, preset).load().then(c => { if (alive) setCfg(c); }).catch(() => { if (alive) setCfg({ regions: {}, activities: [] }); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera.id, preset?.token, reload]);

  if (!ptz) return <>{children(camera, target, 'camera', onSaved)}</>;

  const saved = () => { setReload(n => n + 1); onSaved(); };
  const following = followsPresets(presets);

  async function goto() {
    if (!preset) return;
    setBusy(true);
    try { await ptzApi.gotoPreset(camera.id, preset.token); toast(`Going to ${preset.name || preset.token}`); }
    catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }
  async function takePicture() {
    if (!preset) return;
    setBusy(true);
    try {
      const r = await ptzApi.takeReference(camera.id, preset.token);
      setNight(r.night);
      toast(`${r.night ? 'Night' : 'Day'} picture of ${preset.name || preset.token} taken`);
      setReload(n => n + 1);
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }

  async function review(action: 'accept' | 'undo') {
    if (!preset) return;
    setBusy(true);
    try {
      await ptzApi.reviewZones(camera.id, preset.token, action);
      toast(action === 'accept' ? `${preset.name || preset.token}'s rules run again` : 'Zones put back where they were — check them');
      saved();
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }
  async function copyArchived(a: PtzArchivedView) {
    if (!preset) return;
    const own = !!(preset.ai?.zones || preset.ai?.tripwires || preset.ai?.rules);
    if (own && !window.confirm(`Replace ${preset.name || preset.token}'s own zones and rules with those of deleted “${a.name}”?`)) return;
    setBusy(true);
    try {
      await ptzApi.copyArchivedView(camera.id, a.id, preset.token, own);
      toast(`Zones of “${a.name}” copied — check them on this picture, then press “Zones are right”`);
      saved();
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }
  async function forgetArchived(a: PtzArchivedView) {
    if (!window.confirm(`Forget the zones and rules of deleted preset “${a.name}”? This can't be undone.`)) return;
    try { await ptzApi.forgetArchivedView(camera.id, a.id); setArchived(xs => xs.filter(x => x.id !== a.id)); }
    catch (e: any) { toast(e.message, 'err'); }
  }

  const base = preset ? `/cameras/${camera.id}/ptz/presets/${encodeURIComponent(preset.token)}` : '';
  const rv = preset?.review;
  const reviewBanner = preset && rv && (
    <div role="status" style={{ flexBasis: '100%', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8,
                                 padding: '8px 10px', borderRadius: 6, fontSize: 12.5,
                                 background: rv.paused ? 'rgba(245, 158, 11, 0.12)' : 'rgba(37, 99, 235, 0.08)',
                                 border: `1px solid ${rv.paused ? 'var(--warn, #f59e0b)' : 'var(--accent, #2563eb)'}` }}>
      <span style={{ flex: '1 1 320px' }}>
        <b>{rv.state === 'awaiting_picture' ? 'This preset was re-stored on the camera'
          : rv.paused ? 'Check these zones — this preset’s rules are paused' : 'Zones moved with the view'}</b>
        {' — '}{rv.note}{rv.at ? ` (${rv.reason}, ${when(rv.at)})` : ''}.
        {rv.paused && rv.state !== 'awaiting_picture' && ' Fix them below and save, or confirm they are right.'}
        {rv.state === 'awaiting_picture' && ' Go to the preset and take a picture to line them up now.'}
      </span>
      {preset.has_previous_reference && (
        <button className="btn sm" onClick={() => setShowBefore(b => !b)}>{showBefore ? 'Hide before' : 'Show before'}</button>
      )}
      {rv.can_undo && <button className="btn sm" disabled={busy} onClick={() => review('undo')}>Put zones back</button>}
      {rv.state !== 'awaiting_picture' && (
        <button className="btn sm primary" disabled={busy} onClick={() => review('accept')}>Zones are right</button>
      )}
      {showBefore && preset.has_previous_reference && (
        <div style={{ flexBasis: '100%' }}>
          <div style={{ fontSize: 11.5, color: 'var(--text2)', marginBottom: 4 }}>Before the view changed (the zones were drawn on this):</div>
          <AuthImg path={`${base}/previous-reference`} alt="Before" style={{ maxWidth: 480, width: '100%', borderRadius: 4 }} />
        </div>
      )}
    </div>
  );

  const archivedList = archived.length > 0 && (
    <div style={{ flexBasis: '100%', fontSize: 12 }}>
      <button className="btn sm" onClick={() => setShowArchived(v => !v)}
              title="Zones and rules of presets that were deleted — copy them onto a preset instead of drawing again">
        Deleted presets ({archived.length}) {showArchived ? '▴' : '▾'}
      </button>
      {showArchived && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, marginTop: 8 }}>
          {archived.map(a => (
            <div key={a.id} className="card" style={{ width: 220, padding: 8 }}>
              {a.has_picture && <AuthImg path={`/cameras/${camera.id}/ptz/archived-views/${a.id}/picture`} alt={a.name}
                                         style={{ width: '100%', borderRadius: 4 }} />}
              <div style={{ fontWeight: 600, marginTop: 4 }}>{a.name}</div>
              <div style={{ color: 'var(--text2)', fontSize: 11.5 }}>{aiText(a)} · {a.reason || 'deleted'} {when(a.archived_at)}</div>
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <button className="btn sm" disabled={busy || !preset}
                        title={preset ? `Copy onto ${preset.name || preset.token} — lined up with its picture, for you to check`
                          : 'Pick a preset in View first'}
                        onClick={() => copyArchived(a)}>{preset ? `Copy to ${preset.name || preset.token}` : 'Copy to…'}</button>
                <button className="btn sm" onClick={() => forgetArchived(a)}>Forget</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );

  const bar = (
    <div className="d-hint" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8, marginBottom: 12 }}>
      <b style={{ fontSize: 12.5 }}>View</b>
      <select value={preset ? preset.token : 'camera'} onChange={e => { setNight(false); onView(e.target.value); }}
              title="Each preset is its own view with its own zones and rules">
        <option value="camera">Whole camera{following ? ' (not used while presets have rules)' : ''}</option>
        {(presets || []).map(p => (
          <option key={p.token} value={p.token}>{p.name || `Preset ${p.token}`} — {aiText(p)}{p.review?.paused ? ' — ⚠ check zones' : ''}</option>
        ))}
      </select>
      {preset && <>
        <span style={{ fontSize: 12, color: 'var(--text2)' }}>
          Drawing on {preset.name || preset.token}'s {night ? 'night' : 'day'} picture
          {(night ? preset.reference_night_at : preset.reference_at) ? ` (${when(night ? preset.reference_night_at : preset.reference_at)})` : ' (none yet — the small thumbnail)'}
        </span>
        {preset.reference_at && preset.reference_night_at && (
          <button className="btn sm" onClick={() => setNight(n => !n)}>{night ? 'Show day' : 'Show night'}</button>
        )}
        <button className="btn sm" disabled={busy} onClick={goto}>Go to preset</button>
        <button className="btn sm" disabled={busy} onClick={takePicture}
                title="Take this preset's picture now (the camera must be at the preset). Day and night are kept apart.">
          Take picture now
        </button>
      </>}
      {reviewBanner}
      <div style={{ flexBasis: '100%', fontSize: 11.5, color: 'var(--text3, #8a94a6)' }}>
        {following
          ? 'This camera’s AI follows its presets: a preset’s rules run only while the camera is confirmed there; nothing runs while it moves or between presets.'
          : 'Give a preset zones and rules to make this camera’s AI follow its presets (each preset watched like a fixed camera).'}
      </div>
      {archivedList}
    </div>
  );

  if (preset && cfg === null) return <>{bar}<div className="empty">Loading {preset.name || preset.token}…</div></>;
  const cam = preset ? { ...camera, analytics_config: cfg as AnalyticsConfig } : camera;
  return <>{bar}{children(cam, target, target.key, saved)}</>;
}
