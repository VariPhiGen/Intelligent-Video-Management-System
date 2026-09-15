/**
 * MapToolbar.tsx — the row above the plan: which site plan, which layer, and
 * the manage actions (edit placement, calibrate, rename, delete, upload).
 *
 * Purely presentational; every action is a callback. The manage buttons are
 * rendered only when the caller passes `canManage`, matching the server-side
 * camera_manage gate on the routes behind them.
 */
import type { SitemapMeta } from '@/lib/types';

import type { View } from './mapConstants';

const LAYERS: [View, string][] = [
  ['cameras', 'Cameras'],
  ['ha', 'HA Devices'],
  ['heatmap', 'Heatmap'],
];

export function MapToolbar({
  sitemaps, currentId, currentMap, onSelectMap,
  view, onView,
  canManage, editMode, calibrating, busy,
  onToggleEdit, onToggleCalibrate, onRename, onDelete, onUpload,
}: {
  sitemaps: SitemapMeta[];
  currentId: number | null;
  currentMap: SitemapMeta | null;
  onSelectMap: (id: number) => void;
  view: View;
  onView: (v: View) => void;
  canManage: boolean;
  editMode: boolean;
  calibrating: boolean;
  busy: boolean;
  onToggleEdit: () => void;
  onToggleCalibrate: () => void;
  onRename: () => void;
  onDelete: () => void;
  onUpload: () => void;
}) {
  return (
    <div className="tabrow">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
        <label htmlFor="sitemap-picker" style={{ fontSize: 12, color: 'var(--muted)', flexShrink: 0 }}>
          Site plan
        </label>
        <select id="sitemap-picker" value={currentId ?? ''}
          title={currentMap ? `${currentMap.cameras_placed} camera${currentMap.cameras_placed === 1 ? '' : 's'} placed` : undefined}
          onChange={e => onSelectMap(Number(e.target.value))}
          style={{ minWidth: 180, maxWidth: 320 }}>
          {sitemaps.map(s => (
            <option key={s.id} value={s.id}>
              {s.name} · {s.cameras_placed} camera{s.cameras_placed === 1 ? '' : 's'}
            </option>
          ))}
        </select>
        {sitemaps.length > 1 && (
          <span style={{ fontSize: 11.5, color: 'var(--dim)', flexShrink: 0 }}>
            of {sitemaps.length}
          </span>
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div className="tabs" style={{ marginBottom: 0 }}>
          {LAYERS.map(([key, label]) => (
            <div key={key} className={`tab${view === key ? ' active' : ''}`} onClick={() => onView(key)}>{label}</div>
          ))}
          <div className="tab disabled" title="Coming soon">
            Geofences<span className="nav-soon" style={{ marginLeft: 6 }}>soon</span>
          </div>
        </div>
        {canManage && currentMap && (
          <>
            <button
              className={editMode ? 'btn-primary btn-sm' : 'btn-subtle btn-sm'}
              title={view === 'ha'
                ? 'Place, drag or remove HA peripherals on this map'
                : 'Drag cameras to place/reposition them on this map'}
              onClick={onToggleEdit}>
              {editMode ? '✓ Editing placement' : 'Edit placement'}
            </button>
            <button
              className={calibrating ? 'btn-primary btn-sm' : 'btn-subtle btn-sm'}
              title="Anchor this plan to GPS so cameras auto-place by their coordinates"
              onClick={onToggleCalibrate}>
              {calibrating ? '✓ Calibrating' : (currentMap.calibration ? '⌖ Recalibrate' : '⌖ Calibrate GPS')}
            </button>
            <button className="btn-subtle btn-sm" title="Rename this map" onClick={onRename}>✎</button>
            <button className="btn-subtle btn-sm btn-quiet-danger" title="Delete this map"
              disabled={busy} onClick={onDelete}>🗑</button>
          </>
        )}
        {canManage && <button className="btn-primary btn-sm" onClick={onUpload}>＋ Upload</button>}
      </div>
    </div>
  );
}
