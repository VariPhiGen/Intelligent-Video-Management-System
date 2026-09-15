/**
 * ConfigurationTab — the CMM view: every camera and its configured activities +
 * zones at a glance. Read-only (authoring stays per-camera under Config →
 * Zones & Analytics); this is where you see the whole fleet's analytics config
 * and jump in to edit. Reuses useCameras() — each camera already carries its
 * analytics_config — so there's no extra fetch.
 */
import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCameras } from '@/lib/cameras';
import { useAuth } from '@/lib/auth';
import { useActivityCatalog } from '@/pages/config/analytics/useActivityCatalog';
import { Stat } from './Stat';

export function ConfigurationTab() {
  const nav = useNavigate();
  const { cameras, loading, error } = useCameras();
  const { isAdmin } = useAuth();
  const { catalog, byKey } = useActivityCatalog();
  const [onlyUnconfigured, setOnlyUnconfigured] = useState(false);

  const rows = useMemo(() => cameras.map(c => {
    const cfg = c.analytics_config || { regions: {}, activities: [] };
    const activities = Array.isArray(cfg.activities) ? cfg.activities : [];
    return { c, activities, regionCount: Object.keys(cfg.regions || {}).length };
  }), [cameras]);

  const configured = rows.filter(r => r.activities.length > 0);
  const gap = rows.length - configured.length;
  const totalActivities = configured.reduce((s, r) => s + r.activities.length, 0);
  const totalRegions = configured.reduce((s, r) => s + r.regionCount, 0);
  const coverage = rows.length ? Math.round((configured.length / rows.length) * 100) : 0;

  const shown = onlyUnconfigured ? rows.filter(r => !r.activities.length) : rows;
  // Detection (activities) is configured in AI Config; zones live one tab over.
  const goConfig = (id: string) => nav(`/cameras/config?cam=${id}&tab=ai`);

  return (
    <div className="fade">
      {/* The page title lives in the topbar (as on every other page) — this row
          carries the explanation and the one page-level action. */}
      <div className="page-head">
        <div className="page-head-l">
          <p className="lede">
            Which activities each camera watches for, and where. Authored per camera under
            <b> Cameras → Configuration → AI Config</b>; the analytics pipeline reads these configs directly.
          </p>
        </div>
        {isAdmin && (
          <div className="page-head-actions">
            {/* The catalog itself is administered, not authored here. */}
            <button className="btn-ghost" onClick={() => nav('/admin?tab=types')}>⚙ Manage detection types</button>
          </div>
        )}
      </div>

      {/* Coverage leads — it's the only figure here that can be wrong, so it
          gets the colour and the others stay neutral. */}
      <div className="stat-grid">
        <Stat label="Coverage" value={String(coverage)} unit="%"
              sub={`${configured.length} of ${rows.length || 0} cameras configured`}
              tint={!rows.length ? undefined : gap === 0 ? 'var(--green)' : coverage >= 50 ? 'var(--yellow)' : 'var(--red)'} />
        <Stat label="Activities" value={String(totalActivities)}
              sub={totalActivities ? `across ${configured.length} camera${configured.length === 1 ? '' : 's'}` : 'none configured yet'} />
        <Stat label="Regions" value={String(totalRegions)}
              sub={totalRegions ? 'zones & lines drawn' : 'no zones drawn yet'} />
        <Stat label="Detection types" value={String(catalog.length)} sub="in the shared vocabulary" />
      </div>

      {/* The detection-type vocabulary — the palette everything below draws from. */}
      <div className="panel">
        <div className="panel-head">
          <div className="panel-head-l">
            <div>
              <div className="panel-title">Detection types</div>
              <div className="panel-sub">Shared vocabulary — every activity picks one of these.</div>
            </div>
          </div>
        </div>
        <div className="panel-body">
          {catalog.length ? (
            <div style={{ display: 'flex', gap: 'var(--s2)', flexWrap: 'wrap' }}>
              {catalog.map(t => (
                <span key={t.key} className="chip" style={{ cursor: 'default' }}>
                  <span style={{ width: 9, height: 9, borderRadius: '50%', background: t.color, flexShrink: 0,
                                 boxShadow: `0 0 0 3px color-mix(in srgb, ${t.color} 18%, transparent)` }} />
                  {t.label}
                </span>
              ))}
            </div>
          ) : (
            <div className="emptystate" style={{ padding: 'var(--s6)' }}>
              <div className="glyph">◇</div>
              <h4>No detection types</h4>
              <p>{isAdmin
                ? 'Define the vocabulary before cameras can be given activities to watch for.'
                : 'An administrator defines the vocabulary before cameras can be given activities to watch for.'}</p>
              {isAdmin && <button className="btn-primary btn-sm" onClick={() => nav('/admin?tab=types')}>Define types</button>}
            </div>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div className="panel-head-l">
            <div>
              <div className="panel-title">Per-camera configuration</div>
              <div className="panel-sub">Select a row to open its analytics editor.</div>
            </div>
          </div>
          {gap > 0 && (
            <div className="panel-actions">
              <button className={`chip${onlyUnconfigured ? ' active' : ''}`}
                      onClick={() => setOnlyUnconfigured(v => !v)}>
                <span className="cdot" style={{ background: 'var(--yellow)' }} />
                {gap} not configured
              </button>
            </div>
          )}
        </div>

        <div className="panel-body flush">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Camera</th><th style={{ width: 130 }}>Status</th><th>Activities</th>
                  <th style={{ width: 84 }}>Regions</th><th style={{ width: 110 }}></th>
                </tr>
              </thead>
              <tbody>
                {error && <tr><td colSpan={5}><div className="empty">{error}</div></td></tr>}

                {/* Skeleton rows keep the table's shape while data lands, so
                    nothing jumps when it arrives. */}
                {!error && loading && !cameras.length && [0, 1, 2, 3].map(i => (
                  <tr key={i}>
                    <td><div className="skel-stack"><span className="skel" style={{ width: 132 }} /><span className="skel" style={{ width: 84, height: 9 }} /></div></td>
                    <td><span className="skel" style={{ width: 64 }} /></td>
                    <td><span className="skel" style={{ width: 190 }} /></td>
                    <td><span className="skel" style={{ width: 24 }} /></td>
                    <td />
                  </tr>
                ))}

                {!error && !loading && !cameras.length && (
                  <tr><td colSpan={5} style={{ padding: 0 }}>
                    <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                      <div className="glyph">▣</div>
                      <h4>No cameras registered</h4>
                      <p>Add cameras first — analytics are configured per camera, on the zones they see.</p>
                      <button className="btn-primary btn-sm" onClick={() => nav('/cameras/add')}>＋ Add cameras</button>
                    </div>
                  </td></tr>
                )}

                {!error && !!cameras.length && !shown.length && (
                  <tr><td colSpan={5} style={{ padding: 0 }}>
                    <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                      <div className="glyph">✓</div>
                      <h4>Every camera is configured</h4>
                      <p>Nothing left in the gap — clear the filter to see the whole fleet.</p>
                      <button className="btn-ghost btn-sm" onClick={() => setOnlyUnconfigured(false)}>Show all cameras</button>
                    </div>
                  </td></tr>
                )}

                {shown.map(({ c, activities, regionCount }) => (
                  <tr key={c.id} className="inv-row" title="Open analytics configuration" onClick={() => goConfig(c.id)}>
                    <td>
                      <div style={{ fontWeight: 600 }}>{c.name}</div>
                      <div style={{ color: 'var(--dim)', fontSize: 10.5, fontFamily: 'var(--mono)', marginTop: 1 }}>{c.slug}</div>
                    </td>
                    <td>
                      <span className="dotlabel">
                        <span className="d" style={{
                          background: !c.enabled ? 'var(--dim)' : c.health_status === 'connected' ? 'var(--green)' : 'var(--red)',
                        }} />
                        {!c.enabled ? 'Disabled' : c.health_status === 'connected' ? 'Online' : 'Offline'}
                      </span>
                    </td>
                    <td>
                      {activities.length === 0
                        ? <span style={{ color: 'var(--dim)', fontSize: 12.5, fontStyle: 'italic' }}>Not configured</span>
                        : (
                          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                            {activities.map(a => {
                              const zoneNames = (a.regions || []).map(rid => c.analytics_config?.regions?.[rid]?.name ?? rid);
                              return (
                                <span key={a.type} className="chip" style={{ cursor: 'default', fontSize: 11.5, padding: '3px 10px' }}
                                      title={`${byKey[a.type]?.label ?? a.type} → ${zoneNames.length ? zoneNames.join(', ') : 'no zones'}`}>
                                  <span style={{ width: 7, height: 7, borderRadius: '50%', background: byKey[a.type]?.color ?? 'var(--dim)', flexShrink: 0 }} />
                                  {byKey[a.type]?.label ?? a.type}
                                </span>
                              );
                            })}
                          </div>
                        )}
                    </td>
                    <td style={{ fontFamily: 'var(--mono)', fontSize: 12.5, color: regionCount ? 'var(--text2)' : 'var(--dim)' }}>
                      {regionCount || '—'}
                    </td>
                    {/* Ghost, not primary: a column of orange buttons would be
                        the loudest thing on the page and none of them is THE
                        action — the row itself is already the affordance. */}
                    <td onClick={e => e.stopPropagation()} style={{ textAlign: 'right' }}>
                      <button className="btn-ghost btn-sm" onClick={() => goConfig(c.id)}>
                        {activities.length ? 'Edit' : 'Configure'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

    </div>
  );
}
