/**
 * AnalyticsTab.tsx — "Zones & Analytics": WHERE the camera looks. The frame is
 * the subject, so it leads: pick zone or tripwire from the segmented control and
 * click to place points. The rail is the inventory — name, direction, delete —
 * and each row names the activities watching it, which is the cross-reference
 * that makes a delete here visibly destructive. Pointing at a row lights its
 * shape on the frame and recedes the others.
 *
 * WHAT to detect is configured in AI Config, which references these regions by
 * id — so saving here merges the edited regions over the stored activities
 * (read fresh at save time) and drops any left dangling.
 */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { AnalyticsRegion, Camera } from '@/lib/types';
import { useToast } from '@/components/Toast';
import { PolygonEditor, type EditorPolygon } from '@/components/PolygonEditor';
import { entryArrow, hasEntryDirection } from '@/lib/tripwire';
import { ZONE_COLORS, dimHex } from '../analytics/activities';
import { useActivityCatalog } from '../analytics/useActivityCatalog';
import { buildRegionSave, freeId, freeRegionName, readConfig, watcherCount } from '../analytics/analyticsConfig';

export function AnalyticsTab({ camera, onSaved }: { camera: Camera; onSaved: () => void }) {
  const toast = useToast();
  const { byKey } = useActivityCatalog();
  const [regions, setRegions] = useState<Record<string, AnalyticsRegion>>({});
  const [regionOrder, setRegionOrder] = useState<string[]>([]);
  const [drawing, setDrawing] = useState(false);
  const [drawKind, setDrawKind] = useState<'zone' | 'tripwire'>('zone');
  const [lit, setLit] = useState<string | null>(null);   // region the cursor is on
  const [saving, setSaving] = useState(false);

  // Seed from the stored config; reset only on camera switch (like PrivacyTab).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const { regions: r, regionOrder: ro } = readConfig(camera);
    setRegions(r);
    setRegionOrder(ro);
    setDrawing(false);
  }, [camera.id]);

  const nextRegionColor = ZONE_COLORS[regionOrder.length % ZONE_COLORS.length];

  function onRegionDrawn(points: number[][]) {
    const rid = freeId('region_', x => !!regions[x]);
    // Lowest unused name for the kind (Zone 1, Zone 2, …) so a deletion can't
    // make the next drawing reuse a live name; editable in the Regions list.
    const name = freeRegionName(regions, drawKind);
    // A new tripwire starts with an entry direction, so its arrow is on the
    // frame straight away; the operator flips it in the list if it points the
    // wrong way.
    const region: AnalyticsRegion = drawKind === 'tripwire'
      ? { kind: 'tripwire', name, color: nextRegionColor, points, direction: 'a2b' }
      : { kind: 'zone', name, color: nextRegionColor, points };
    setRegions(r => ({ ...r, [rid]: region }));
    setRegionOrder(o => [...o, rid]);
    setDrawing(false);
  }

  const renameRegion = (rid: string, name: string) => setRegions(r => ({ ...r, [rid]: { ...r[rid], name } }));
  const setDirection = (rid: string, direction: 'both' | 'a2b' | 'b2a') =>
    setRegions(r => ({ ...r, [rid]: { ...r[rid], direction } }));

  function removeRegion(rid: string) {
    // Activities in AI Config reference regions by id; the API rejects dangling
    // references, so warn with the stored count before orphaning them.
    const watchers = watcherCount(camera.analytics_config, rid);
    if (watchers > 0) {
      // An activity's region is required and must key into `regions` (backend
      // model), so a watching activity cannot survive as "unassigned" — it is
      // dropped by buildRegionSave. Say so plainly rather than downplaying it.
      const what = `${regions[rid]?.name ?? 'This region'} is watched by ${watchers} ` +
        `${watchers === 1 ? 'activity' : 'activities'} — ${watchers === 1 ? 'it' : 'they'} will be removed from this camera's detection config. Continue?`;
      if (!window.confirm(what)) return;
    }
    setRegions(r => { const n = { ...r }; delete n[rid]; return n; });
    setRegionOrder(o => o.filter(x => x !== rid));
  }

  // Every stored region needs a non-blank name (backend requires 1–64 chars);
  // trailing/leading whitespace is trimmed only at save time, not per keystroke.
  const namesOk = regionOrder.every(rid => !regions[rid] || regions[rid].name.trim().length > 0);

  async function save() {
    setSaving(true);
    try {
      // Read the stored config fresh right before building the payload — the
      // camera prop only refreshes via ConfigPage's fire-and-forget refresh()
      // (plus a 15s poll that can fail silently), so a stale copy here could
      // silently delete activities AI Config just saved. Fail loudly instead.
      const fresh = await apiFetch<Camera>(`/cameras/${camera.id}`);
      const trimmed: Record<string, AnalyticsRegion> = {};
      regionOrder.forEach(rid => { if (regions[rid]) trimmed[rid] = { ...regions[rid], name: regions[rid].name.trim() }; });
      const payload = buildRegionSave(fresh.analytics_config, trimmed, regionOrder);
      await apiFetch(`/cameras/${camera.id}/analytics`, { method: 'PUT', body: JSON.stringify(payload) });
      toast('Zones saved');
      onSaved();
    } catch (e: any) {
      // buildRegionSave carries stored activities through verbatim — if one of
      // them references a type an admin has since deleted from the catalog,
      // the backend 422s with "Unknown activity type(s): …". We don't
      // pre-validate against the (possibly stale offline) catalog, so this is
      // only caught here — surface the cause and the fix instead of the raw
      // API text, and leave drawn zones in state so nothing is lost.
      const msg = /unknown activity type/i.test(e?.message ?? '')
        ? "This camera has an activity whose detection type no longer exists. Fix it under AI Config, then save your zones again."
        : e.message;
      toast(msg, 'err');
    }
    setSaving(false);
  }

  // Which shape the operator is pointing at, from a region row. Nothing pointed
  // = every shape at full strength; otherwise the rest recede, so a dense frame
  // stays readable while you work on one zone.
  const editorShapes: EditorPolygon[] = regionOrder.filter(rid => regions[rid]).map(rid => {
    const r = regions[rid];
    const on = !lit || lit === rid;
    return {
      points: r.points, color: on ? r.color : dimHex(r.color), label: on ? r.name : undefined,
      kind: r.kind, direction: r.direction,
    };
  });

  const zoneCount = regionOrder.filter(r => regions[r]?.kind === 'zone').length;
  const wireCount = regionOrder.filter(r => regions[r]?.kind === 'tripwire').length;

  /** Names of the stored activities watching a region — the cross-reference the
   *  split would otherwise lose, and the reason a delete here is destructive. */
  const watchersOf = (rid: string) => (camera.analytics_config?.activities || [])
    .filter(a => (a.regions || []).includes(rid))
    .map(a => byKey[a.type]?.label ?? a.type);

  return (
    <div className="cfg wide">
      {/* ── Work column: the frame is the subject ── */}
      <div>
        <div className="cfg-head">
          <div className="cfg-head-l">
            <span className="cfg-title">Camera frame</span>
            <span className="cfg-sub">
              {drawing
                ? `Click the frame to place your ${drawKind === 'tripwire' ? 'tripwire' : 'zone'}`
                : 'Draw where this camera should look'}
            </span>
          </div>
          <div className="ch-seg" role="group" aria-label="What to draw">
            <button className={drawKind === 'zone' ? 'active' : ''}
                    onClick={() => { setDrawKind('zone'); setDrawing(true); }}>▱ Zone</button>
            <button className={drawKind === 'tripwire' ? 'active' : ''}
                    onClick={() => { setDrawKind('tripwire'); setDrawing(true); }}>⟋ Tripwire</button>
          </div>
        </div>

        <PolygonEditor cameraId={camera.id} polygons={editorShapes}
          drawing={drawing}
          drawMode={drawKind === 'tripwire' ? 'line' : 'polygon'}
          drawColor={drawing ? nextRegionColor : undefined}
          noun={drawKind}
          onFinish={onRegionDrawn} onCancel={() => setDrawing(false)} />

        {!drawing && (
          <div className="cfg-note" style={{ marginTop: 'var(--s4)', marginBottom: 0 }}>
            <span>▱</span>
            <span>
              A <b>zone</b> is an area to watch — loitering, PPE, crowding. A <b>tripwire</b> is a gateway
              line people cross. Pick one above, then click the frame. What gets detected in each is set
              under <a href={`#/cameras/config?cam=${camera.id}&tab=ai`}>AI Config</a>.
            </span>
          </div>
        )}
        {(drawKind === 'tripwire' || wireCount > 0) && (
          <div className="cfg-note" style={{ marginTop: 'var(--s3)', marginBottom: 0 }}>
            <span>⟂</span>
            <span>
              <b>Entry / exit tripwires.</b> Draw the tripwire across the path where people will cross. Set
              the direction arrow to point toward the area you consider Entry. People crossing in the arrow
              direction are logged as <b>Entry</b>; people crossing in the opposite direction are logged
              as <b>Exit</b>. The dashed line is the gateway; the arrow is the way people walk in — change
              it with the <b>Entry</b> selector in the list.
            </span>
          </div>
        )}

        <div className="cfg-savebar">
          <button className="btn-primary" disabled={saving || !namesOk} onClick={save}>
            {saving ? 'Saving…' : 'Save zones'}
          </button>
          {!namesOk
            ? <span className="cfg-hint warn">Every zone needs a name</span>
            : <span className="cfg-hint">
                {zoneCount + wireCount === 0
                  ? 'Nothing drawn yet.'
                  : `${zoneCount} zone${zoneCount === 1 ? '' : 's'}${wireCount ? ` · ${wireCount} tripwire${wireCount === 1 ? '' : 's'}` : ''} on this camera.`}
              </span>}
        </div>
      </div>

      {/* ── Rail: the inventory, and who watches what ── */}
      <aside className="cfg-rail">
        <div className="panel" style={{ marginBottom: 0 }}>
          <div className="panel-head">
            <div className="panel-head-l">
              <div>
                <div className="panel-title">Zones &amp; tripwires</div>
                <div className="panel-sub">{regionOrder.length ? 'Hover to locate on the frame' : 'Nothing drawn yet'}</div>
              </div>
            </div>
            {regionOrder.length > 0 && <span className="cfg-count">{regionOrder.length}</span>}
          </div>
          <div className="panel-body" style={{ padding: 'var(--s3)' }}>
            {!regionOrder.length ? (
              <div className="cfg-empty" style={{ padding: 'var(--s6) var(--s4)' }}>
                <div className="glyph">▱</div>
                <h4>An empty field of view</h4>
                <p>Pick <b>Zone</b> or <b>Tripwire</b> above the frame and click to place points. Names are
                   editable here afterwards.</p>
              </div>
            ) : regionOrder.map(rid => regions[rid] && (
              <div key={rid} className={`rgn${lit === rid ? ' focus' : ''}`}
                   onMouseEnter={() => setLit(rid)} onMouseLeave={() => setLit(null)}>
                <span className="rgn-kind" style={{ background: regions[rid].color }}
                      title={regions[rid].kind === 'tripwire' ? 'Tripwire' : 'Zone'}>
                  {regions[rid].kind === 'tripwire' ? '⟋' : '▱'}
                </span>
                <input className={`rgn-name${regions[rid].name.trim() ? '' : ' invalid'}`}
                       value={regions[rid].name} maxLength={64}
                       aria-label="Zone name"
                       onChange={e => renameRegion(rid, e.target.value)} />
                <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                  {regions[rid].kind === 'tripwire' && (
                    <select className="rgn-dir" value={regions[rid].direction ?? 'both'}
                            aria-label="Entry direction"
                            title="The way people walk when they enter. Crossing the other way is an Exit."
                            onChange={e => setDirection(rid, e.target.value as 'both' | 'a2b' | 'b2a')}>
                      <option value="a2b">Entry {entryArrow(regions[rid].points, 'a2b') ?? 'A→B'}</option>
                      <option value="b2a">Entry {entryArrow(regions[rid].points, 'b2a') ?? 'B→A'}</option>
                      <option value="both">Not set</option>
                    </select>
                  )}
                  <button className="cfg-icon danger rgn-del" title="Remove" onClick={() => removeRegion(rid)}>✕</button>
                </span>
                <span className="rgn-watch">
                  {regions[rid].kind === 'tripwire' && !hasEntryDirection(regions[rid].direction) && (
                    <><b style={{ color: 'var(--yellow)' }}>No entry direction — Entry / Exit does not count this line.</b>{' '}</>
                  )}
                  {watchersOf(rid).length
                    ? <>watched by <b>{watchersOf(rid).join(', ')}</b></>
                    : 'nothing watches this yet'}
                </span>
              </div>
            ))}
          </div>
        </div>
      </aside>
    </div>
  );
}
