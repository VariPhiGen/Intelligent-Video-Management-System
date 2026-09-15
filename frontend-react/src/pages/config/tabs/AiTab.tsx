/**
 * AiTab.tsx — "AI Config": WHAT this camera detects. The work column is the
 * activity list (each a catalog type watching one or more zones, with its own
 * detection settings); the rail answers "where does that actually look" with a read-only
 * coverage map of this camera's zones, plus the motion-detection engine, which
 * is a separate setting on a separate endpoint.
 *
 * Pointing at a row lights its zone on the map and recedes the others — the one
 * interaction that keeps this tab and Zones & Analytics legible as a pair.
 *
 * Zones are drawn in Zones & Analytics; this tab only references them, so it
 * saves the analytics contract by merging its activities over the stored
 * regions, read fresh at save time.
 */
import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { apiFetch } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import { PolygonEditor, type EditorPolygon } from '@/components/PolygonEditor';
import { useActivityCatalog } from '../analytics/useActivityCatalog';
import { dimHex, isAvailable, zoneOptional } from '../analytics/activities';
import { ActivityRow } from '../analytics/ActivityRow';
import { PreviewModal } from '../analytics/PreviewModal';
import {
  buildActivitySave, defaultParams, readConfig, schedulePart,
  type EditActivity,
} from '../analytics/analyticsConfig';

const label: CSSProperties = { fontSize: 11.5, color: 'var(--muted)' };

export function AiTab({ camera, motionState, onSaved }: {
  camera: Camera;
  motionState?: string;
  onSaved: () => void;
}) {
  const toast = useToast();
  // The catalog is read-only here — admins edit it under Administration → Activity types.
  const { catalog, byKey } = useActivityCatalog();

  // ── motion detection (unchanged) ──
  const [motion, setMotion] = useState(camera.motion_detection === true);
  const [sens, setSens] = useState<string>(camera.motion_sensitivity || 'medium');
  const [savingMotion, setSavingMotion] = useState(false);

  // ── Smart Search indexing (opt-OUT: on unless excluded) ──
  const [indexing, setIndexing] = useState(camera.search_indexing !== false);
  const [domains, setDomains] = useState<string[]>(
    camera.search_domains ?? ['person', 'vehicles']
  );
  const [savingIndexing, setSavingIndexing] = useState(false);

  async function saveIndexing(next: boolean) {
    setSavingIndexing(true);
    try {
      await apiFetch(`/cameras/${camera.id}`, {
        method: 'PUT',
        body: JSON.stringify({ search_indexing: next }),
      });
      setIndexing(next);
      toast(next ? 'Smart Search indexing on' : 'Smart Search indexing off');
      onSaved();
    } catch (e: any) { toast(e.message, 'err'); setIndexing(!next); }
    setSavingIndexing(false);
  }

  async function toggleDomain(d: string, on: boolean) {
    const next = on ? [...new Set([...domains, d])] : domains.filter(x => x !== d);
    const before = domains;
    setDomains(next);
    setSavingIndexing(true);
    try {
      await apiFetch(`/cameras/${camera.id}`, {
        method: 'PUT',
        body: JSON.stringify({ search_domains: next }),
      });
      onSaved();
    } catch (e: any) { toast(e.message, 'err'); setDomains(before); }
    setSavingIndexing(false);
  }

  async function saveMotion() {
    setSavingMotion(true);
    try {
      await apiFetch(`/cameras/${camera.id}`, {
        method: 'PUT',
        body: JSON.stringify({ motion_detection: motion, motion_sensitivity: sens }),
      });
      toast('Motion settings saved');
      onSaved();
    } catch (e: any) { toast(e.message, 'err'); }
    setSavingMotion(false);
  }

  // ── activities ── (one card per type, so `type` is the row's identity)
  const [activities, setActivities] = useState<EditActivity[]>([]);
  const [paramsOpenFor, setParamsOpenFor] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [savingActs, setSavingActs] = useState(false);
  // Types whose drawer currently holds an unparseable json-kind field — save is
  // blocked while any row is in this set, same as a zone-less row.
  const [invalidTypes, setInvalidTypes] = useState<Set<string>>(new Set());

  // Regions are read-only here — drawn in Zones & Analytics.
  const { regions, regionOrder } = useMemo(() => readConfig(camera), [camera]);
  const noZones = regionOrder.length === 0;

  // Seed from the stored config; reset only on camera switch (like PrivacyTab).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    setActivities(readConfig(camera).activities);
    setParamsOpenFor(null);
    setInvalidTypes(new Set());
  }, [camera.id]);

  // If a region an activity points at disappears from underneath us (e.g. the
  // operator deleted it in Zones & Analytics and this tab's poll picked up the
  // refresh), drop it from that row's zone set rather than leaving it silently
  // pointed at a region that no longer exists — Save would otherwise drop the
  // activity without any visible change. Returns the same array when nothing
  // needs repairing, so this cannot loop.
  // A row whose zones have ALL gone is removed rather than left zone-less —
  // for a whole-frame-capable type that would silently widen it to everything.
  useEffect(() => {
    setActivities(as => as.some(a => a.regions.some(rid => !regions[rid]))
      ? as.map(a => ({ ...a, regions: a.regions.filter(rid => !!regions[rid]) }))
          .filter((a, i) => a.regions.length > 0 || as[i].regions.length === 0)
      : as);
  }, [regions]);

  const usedTypes = new Set(activities.map(a => a.type));
  // Only what the analytics engine actually runs can be added.
  const availableTypes = catalog.filter(c => isAvailable(c) && !usedTypes.has(c.key));
  // With no zone drawn, only an activity that watches the whole frame can go on.
  const addableTypes = noZones ? availableTypes.filter(zoneOptional) : availableTypes;

  function addActivity() {
    const t = addableTypes[0];
    if (!catalog.length) { toast('No detection types defined — an admin adds them under Administration → Activity types', 'err'); return; }
    // Unreachable from the UI (the button is disabled below) — a defensive
    // no-op rather than a third place this message is delivered.
    if (!t) return;
    setActivities(a => [...a, { type: t.key, regions: [], params: defaultParams(t) }]);
  }
  function setType(type: string, next: string) {
    setActivities(as => as.map(a => a.type === type
      ? { ...a, type: next, params: { ...defaultParams(byKey[next]), ...schedulePart(a.params) } } : a));
    // The row remounts under its new type key (ActivityRow is keyed by type),
    // discarding any draft it held — drop its old invalid flag too, or a fixed
    // or abandoned bad draft could block Save forever under a name nothing
    // still shows.
    if (paramsOpenFor === type) setParamsOpenFor(next);
    setInvalidTypes(s => { if (!s.has(type)) return s; const n = new Set(s); n.delete(type); return n; });
  }
  const addZone = (type: string, rid: string) =>
    setActivities(as => as.map(a => a.type === type && !a.regions.includes(rid)
      ? { ...a, regions: [...a.regions, rid] } : a));
  const removeZone = (type: string, rid: string) =>
    setActivities(as => as.map(a => a.type === type ? { ...a, regions: a.regions.filter(r => r !== rid) } : a));
  const patchParams = (type: string, patch: Record<string, any>) =>
    setActivities(as => as.map(a => a.type === type ? { ...a, params: { ...a.params, ...patch } } : a));
  const toggleDay = (type: string, d: string) => setActivities(as => as.map(a => a.type === type
    ? { ...a, params: { ...a.params, active_days: a.params.active_days.includes(d)
        ? a.params.active_days.filter(x => x !== d) : [...a.params.active_days, d] } } : a));
  function removeActivity(type: string) {
    setActivities(as => as.filter(a => a.type !== type));
    if (paramsOpenFor === type) setParamsOpenFor(null);
    setInvalidTypes(s => { if (!s.has(type)) return s; const n = new Set(s); n.delete(type); return n; });
  }
  const setInvalidFor = (type: string, invalid: boolean) => setInvalidTypes(s => {
    const has = s.has(type);
    if (invalid === has) return s;
    const n = new Set(s);
    invalid ? n.add(type) : n.delete(type);
    return n;
  });

  const canSave = activities.every(a => (a.regions.length > 0 || zoneOptional(byKey[a.type]))
      && a.regions.every(rid => !!regions[rid]))
    && invalidTypes.size === 0;
  const payload = useMemo(
    () => buildActivitySave(camera.analytics_config, activities, byKey),
    [camera.analytics_config, activities, byKey],
  );

  async function saveActivities() {
    setSavingActs(true);
    try {
      // Read the stored config fresh right before building the payload — the
      // camera prop only refreshes via ConfigPage's fire-and-forget refresh()
      // (plus a 15s poll that can fail silently), so a stale copy here could
      // silently delete zones Zones & Analytics just saved. Fail loudly instead.
      const fresh = await apiFetch<Camera>(`/cameras/${camera.id}`);
      const freshPayload = buildActivitySave(fresh.analytics_config, activities, byKey);
      await apiFetch(`/cameras/${camera.id}/analytics`, { method: 'PUT', body: JSON.stringify(freshPayload) });
      toast('Analytics configuration saved');
      onSaved();
    } catch (e: any) { toast(e.message, 'err'); }
    setSavingActs(false);
  }

  const zoneCount = regionOrder.filter(r => regions[r]?.kind === 'zone').length;
  const wireCount = regionOrder.length - zoneCount;

  // Which shape the operator is pointing at, from a row or a legend tag. Nothing
  // pointed = every shape at full strength; otherwise the rest recede.
  const [lit, setLit] = useState<string[] | null>(null);
  const mapShapes: EditorPolygon[] = regionOrder.filter(rid => regions[rid]).map(rid => {
    const r = regions[rid];
    const on = !lit || lit.includes(rid);
    // No labels: at rail width the canvas would scale 11px text down to ~4px.
    // The legend below names the shapes, and lighting one is the pointer.
    return {
      points: r.points, color: on ? r.color : dimHex(r.color),
      kind: r.kind, direction: r.direction,
    };
  });

  /** How many activities include this zone, for the legend tags. */
  const watchers = (rid: string) => activities.filter(a => a.regions.includes(rid)).length;

  return (
    <div className="cfg">
      {/* ── Work column: what this camera detects ── */}
      <div>
        <div className="cfg-head">
          <div className="cfg-head-l">
            <span className="cfg-title">What this camera detects</span>
            {activities.length > 0 && <span className="cfg-count">{activities.length}</span>}
          </div>
          <div className="cfg-actions">
            <button className="btn-primary btn-sm" disabled={!addableTypes.length}
                    onClick={addActivity}>
              ＋ Add activity
            </button>
          </div>
        </div>

        {/* Blockers first, in the order they have to be cleared. */}
        {!catalog.length && (
          <div className="cfg-note act-required">
            <span>◇</span>
            <span>
              <b>No detection types yet.</b> An administrator defines the shared vocabulary under{' '}
              <a href="#/admin?tab=types">Administration → Activity types</a>.
            </span>
          </div>
        )}
        {!!catalog.length && !catalog.some(isAvailable) && (
          <div className="cfg-note act-required">
            <span>◇</span>
            <span>
              <b>No activity is available yet.</b> Activities come from the analytics engine; none is running or reachable right now.
            </span>
          </div>
        )}
        {noZones && availableTypes.length > 0 && (
          <div className={`cfg-note${addableTypes.length ? '' : ' act-required'}`}>
            <span>▱</span>
            <span>
              {addableTypes.length
                ? <><b>No zones drawn.</b> Activities that can watch the whole frame can be added now; the others need a zone from{' '}</>
                : <><b>Draw a zone first.</b> These activities watch one or more zones — draw one under{' '}</>}
              <a href={`#/cameras/config?cam=${camera.id}&tab=analytics`}>Zones &amp; Analytics</a>.
            </span>
          </div>
        )}
        {/* Neutral status, not a blocker — nothing needs fixing here, so this
            does NOT get the accent-tinted .act-required treatment. */}
        {!noZones && !!catalog.length && !availableTypes.length && activities.length > 0 && (
          <div className="cfg-note">
            <span>✓</span>
            <span>Every detection type is already configured on this camera.</span>
          </div>
        )}

        {activities.length === 0 ? (
          <div className="cfg-empty">
            <div className="glyph">◎</div>
            <h4>Nothing to watch for yet</h4>
            <p>
              {!catalog.length
                ? 'Activities come from the analytics engine. Once it is reachable, you can add them to this camera.'
                : noZones
                  ? 'This camera has no zones yet. Add an activity that watches the whole frame, or draw a zone first.'
                  : 'Pick an activity and the zone(s) it should watch. Each gets its own schedule and settings.'}
            </p>
            <button className="btn-primary btn-sm" disabled={!addableTypes.length} onClick={addActivity}>
              ＋ Add activity
            </button>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s3)' }}>
            {activities.map(a => (
              // Keyed by camera too: an unrelated activity of the same type on
              // a DIFFERENT camera must fully remount rather than reuse this
              // component instance, or a json-field draft / invalid flag left
              // over from editing THIS camera could silently leak into the
              // next one's row of the same type.
              <ActivityRow key={`${camera.id}:${a.type}`} act={a}
                catalog={catalog.filter(c => c.key === a.type || (isAvailable(c) && !usedTypes.has(c.key)))} byKey={byKey}
                regions={regions} regionOrder={regionOrder}
                paramsOpen={paramsOpenFor === a.type}
                onToggleParams={() => setParamsOpenFor(paramsOpenFor === a.type ? null : a.type)}
                onSetType={t => setType(a.type, t)}
                onAddZone={rid => addZone(a.type, rid)}
                onRemoveZone={rid => removeZone(a.type, rid)}
                onPatchParams={patch => patchParams(a.type, patch)}
                onToggleDay={d => toggleDay(a.type, d)}
                onRemove={() => removeActivity(a.type)}
                onInvalidChange={bad => setInvalidFor(a.type, bad)}
                onPoint={setLit} />
            ))}
          </div>
        )}

        <div className="cfg-savebar">
          <button className="btn-primary" disabled={savingActs || !canSave} onClick={saveActivities}>
            {savingActs ? 'Saving…' : 'Save configuration'}
          </button>
          <button className="btn-ghost" disabled={!activities.length} onClick={() => setPreviewOpen(true)}>
            Preview
          </button>
          {invalidTypes.size > 0 ? (
            <span className="cfg-hint warn">
              Fix the invalid JSON in {[...invalidTypes].map(t => byKey[t]?.label ?? t).join(', ')} before saving
            </span>
          ) : !canSave && activities.length > 0 ? (
            <span className="cfg-hint warn">Some activities need at least one zone — add one, or remove the activity</span>
          ) : (
            <span className="cfg-hint">Saved settings are picked up by the analytics pipeline directly.</span>
          )}
        </div>
      </div>

      {/* ── Rail: where those activities look, plus the motion engine ── */}
      <aside className="cfg-rail">
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head">
            <div className="panel-head-l">
              <div>
                <div className="panel-title">Coverage</div>
                <div className="panel-sub">Zones this camera watches</div>
              </div>
            </div>
            <a className="cfg-hint" href={`#/cameras/config?cam=${camera.id}&tab=analytics`}>Edit zones</a>
          </div>
          <div className="panel-body">
            <div className="cov">
              <PolygonEditor cameraId={camera.id} polygons={mapShapes} drawing={false} readOnly
                onFinish={() => { /* read-only map — drawing lives in Zones & Analytics */ }} />

              {regionOrder.length > 0 && (
                <div className="cov-legend">
                  {regionOrder.filter(rid => regions[rid]).map(rid => {
                    const n = watchers(rid);
                    return (
                      <span key={rid} className={`cov-tag${lit?.includes(rid) ? ' lit' : n ? '' : ' idle'}`}
                            onMouseEnter={() => setLit([rid])} onMouseLeave={() => setLit(null)}>
                        <i style={{ background: regions[rid].color }} />
                        {regions[rid].name}
                        <span style={{ color: 'var(--dim)' }}>{n || '—'}</span>
                      </span>
                    );
                  })}
                </div>
              )}

              <div className="cov-figs">
                <div className="cov-fig">
                  <b>{activities.length}</b>
                  <span>{activities.length === 1 ? 'Activity' : 'Activities'}</span>
                </div>
                <div className="cov-fig">
                  <b>{zoneCount}<span style={{ color: 'var(--dim)', fontSize: 13, fontWeight: 550 }}>{wireCount ? ` +${wireCount}⟋` : ''}</span></b>
                  <span>{zoneCount === 1 ? 'Zone' : 'Zones'}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <div className="panel-head-l">
              <div>
                <div className="panel-title">Smart Search indexing</div>
                <div className="panel-sub">People and vehicles, searchable by description</div>
              </div>
            </div>
          </div>
          <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s3)' }}>
            <div>
              <label style={label}>Indexing</label>
              <select
                value={String(indexing)}
                disabled={savingIndexing}
                onChange={e => saveIndexing(e.target.value === 'true')}
                style={{ marginTop: 6 }}
              >
                <option value="true">On — index this camera (default)</option>
                <option value="false">Off — exclude from Smart Search</option>
              </select>
            </div>
            {indexing && (
              <div>
                <label style={label}>Index what</label>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 6 }}>
                  {([['person', 'People'],
                     ['vehicles', 'Vehicles'],
                     ['plate', 'Number plates'],
                     ['face', 'Faces']] as const).map(([key, lab]) => (
                    <label key={key} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      <input
                        type="checkbox"
                        checked={domains.includes(key)}
                        disabled={savingIndexing}
                        onChange={e => toggleDomain(key, e.target.checked)}
                      />
                      <span>{lab}</span>
                    </label>
                  ))}
                </div>
                {/* Not a performance control, and saying so stops it being
                    treated as one: the detector costs the same either way
                    (measured). What narrowing saves is disk, and plate reading,
                    which is a second model over every vehicle. */}
                <div className="hint" style={{ marginTop: 6 }}>
                  {domains.length === 0
                    ? 'Nothing selected — this camera will not be indexed at all.'
                    : 'Narrowing saves storage, not detection time. Number plates also '
                      + 'need plate-detection weights configured on the index service.'}
                </div>
                {/* SAID BEFORE IT IS SWITCHED ON, not discovered afterwards.
                    Measured on real footage: a usable face appears on about 4%
                    of the people who walk past, 17% on a close indoor camera
                    and 0.2% on a distant one. An operator who enables this on a
                    gate camera and finds an empty gallery would reasonably
                    conclude the feature is broken. */}
                {domains.includes('face') && (
                  <div className="hint" style={{ marginTop: 6 }}>
                    Faces are stored only when the face is at least ~40 pixels
                    wide, which in practice means close range — on a typical
                    gate camera only a few percent of people yield one. Face
                    images are biometric personal data: they follow this
                    camera's retention and are erased with it.
                  </div>
                )}
              </div>
            )}
            {/* Say the consequence plainly. An excluded camera is not "quiet" —
                it is unsearchable, and Smart Search will label it as such
                rather than returning an empty result that looks like an answer. */}
            <div className="hint">
              {indexing
                ? 'Crops are sampled from the relay, so this adds no load to the camera. Recording is unaffected either way.'
                : 'Excluded. Searches will report this camera as not indexed rather than returning no results — footage is still recorded and playable.'}
            </div>
          </div>
        </div>

        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head">
            <div className="panel-head-l">
              <div>
                <div className="panel-title">Motion detection</div>
                <div className="panel-sub">Movement anywhere in frame</div>
              </div>
            </div>
            {camera.motion_detection && (
              <span className={`badge ${motionState === 'TRIGGERED' ? 'badge-red' : motionState === 'MONITORING' ? 'badge-green' : 'badge-gray'}`}>
                <span className="badge-dot" />{motionState || 'syncing…'}
              </span>
            )}
          </div>
          <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s3)' }}>
            <div>
              <label style={label}>Detection</label>
              <select value={String(motion)} onChange={e => setMotion(e.target.value === 'true')} style={{ marginTop: 6 }}>
                <option value="false">Off</option>
                <option value="true">On — analyze this camera</option>
              </select>
            </div>
            <div>
              <label style={label}>Sensitivity</label>
              <select value={sens} onChange={e => setSens(e.target.value)} disabled={!motion} style={{ marginTop: 6 }}>
                <option value="low">Low — busy or outdoor scenes</option>
                <option value="medium">Medium (default)</option>
                <option value="high">High — quiet indoor scenes</option>
              </select>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s3)', flexWrap: 'wrap' }}>
              <button className="btn-primary btn-sm" disabled={savingMotion} onClick={saveMotion}>
                {savingMotion ? 'Saving…' : 'Save motion settings'}
              </button>
              <span className="cfg-live"><i />Reads the relay, not the camera</span>
            </div>
          </div>
        </div>
      </aside>

      <PreviewModal open={previewOpen} onClose={() => setPreviewOpen(false)}
        camera={camera} config={payload} byKey={byKey} totalEdited={activities.length} />
    </div>
  );
}
