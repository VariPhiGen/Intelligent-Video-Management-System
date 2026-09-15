/**
 * MapPanels.tsx — the Map tab's right column: the georeference card, the two
 * edit-mode trays, and the two detail panels (camera / HA device).
 *
 * All presentational. Every mutation is a callback the page supplies, so these
 * can be read without knowing how placement persists.
 */
import { useEffect, useState } from 'react';

import type { Camera } from '@/lib/types';
import { zoneOf } from '@/lib/cameras';

import { parseGps } from './mapHelpers';
import { STATUS_BADGE } from './mapConstants';
import { HaControlPanel, type HaPin } from './MockLayers';
import type { Device } from '../peripherals/peripheralsData';

/** Georeferencing panel: collect 2–3 control points (marked on the plan) and
 * their real-world lat/lng, then save. The plan-click that adds a point lives
 * in MapPage's onSurfaceClick; this card edits the lat/lng and saves. */
export function CalibrateCard({ points, onChange, onSave, onClear, onCancel, hasExisting, busy }: {
  points: { x: number; y: number; lat: string; lng: string }[];
  onChange: (p: { x: number; y: number; lat: string; lng: string }[]) => void;
  onSave: () => void; onClear: () => void; onCancel: () => void;
  hasExisting: boolean; busy: boolean;
}) {
  const set = (i: number, k: 'lat' | 'lng', v: string) =>
    onChange(points.map((p, j) => (j === i ? { ...p, [k]: v } : p)));
  const remove = (i: number) => onChange(points.filter((_, j) => j !== i));
  const complete = points.length === 2 || points.length === 3;
  return (
    <div className="panel" style={{ marginBottom: 0 }}>
      <div className="panel-head">
        <div className="panel-head-l"><div className="panel-title">Georeference</div></div>
      </div>
      <div className="panel-body">
        <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 10 }}>
          Click a known spot on the plan, then type its GPS. Add 2 points (or 3 for
          a tilted plan). Cameras carrying GPS then place themselves.
        </div>
        {points.length === 0 && (
          <div style={{ fontSize: 12, color: 'var(--dim)', marginBottom: 10 }}>
            No points yet — click the plan to add one.
          </div>
        )}
        {points.map((p, i) => (
          <div key={i} style={{ marginBottom: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
              <span style={{ fontSize: 12, fontWeight: 650 }}>Point {i + 1}</span>
              <button className="btn-subtle btn-sm" onClick={() => remove(i)}>✕</button>
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              <input value={p.lat} onChange={e => set(i, 'lat', e.target.value)} placeholder="lat" style={{ width: '100%' }} />
              <input value={p.lng} onChange={e => set(i, 'lng', e.target.value)} placeholder="lng" style={{ width: '100%' }} />
            </div>
          </div>
        ))}
        <div style={{ display: 'flex', gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
          <button className="btn-primary btn-sm" disabled={busy || !complete} onClick={onSave}>
            {busy ? <span className="spinner" /> : 'Save calibration'}
          </button>
          <button className="btn-subtle btn-sm" onClick={onCancel}>Cancel</button>
          {hasExisting && <button className="btn-danger btn-sm" disabled={busy} onClick={onClear}>Clear</button>}
        </div>
      </div>
    </div>
  );
}

/** Inline GPS editor for the camera detail panel (edit mode). Non-blocking
 * validity hint; Save is disabled only when the value is non-empty but
 * unparseable (empty = clear the field). */
function GpsEditor({ initial, onSave }: { initial: string; onSave: (v: string) => void }) {
  const [val, setVal] = useState(initial);
  useEffect(() => { setVal(initial); }, [initial]);
  const parsed = parseGps(val);
  const dirty = val.trim() !== initial.trim();
  const invalid = val.trim() !== '' && !parsed;
  return (
    <div>
      <div style={{ display: 'flex', gap: 6 }}>
        <input value={val} onChange={e => setVal(e.target.value)} placeholder="28.6139, 77.2090" style={{ width: '100%' }} />
        <button className="btn-primary btn-sm" disabled={!dirty || invalid} onClick={() => onSave(val)}>Save</button>
      </div>
      {invalid && (
        <div style={{ fontSize: 11, color: 'var(--yellow)', marginTop: 3 }}>
          Enter as "lat, lng" — e.g. 28.6139, 77.2090
        </div>
      )}
    </div>
  );
}

/** Edit-mode tray for cameras: the ones assigned to this map but not yet
 *  placed, plus the picker that assigns another camera to it. */
export function CameraTray({ unplaced, unassigned, placingId, onArm, onAdd }: {
  unplaced: Camera[];
  unassigned: Camera[];
  placingId: string | null;
  onArm: (id: string) => void;
  onAdd: (id: string) => void;
}) {
  return (
    <div className="panel" style={{ marginBottom: 0 }}>
      <div className="panel-head">
        <div className="panel-head-l"><div className="panel-title">On this map</div></div>
      </div>
      <div className="panel-body">
        {unplaced.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--dim)', marginBottom: 12 }}>
            Every camera assigned to this map is placed.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 14 }}>
            {unplaced.map(cam => (
              <div key={cam.id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                <span style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {cam.name}
                </span>
                <button
                  className={placingId === cam.id ? 'btn-primary btn-sm' : 'btn-subtle btn-sm'}
                  style={{ flexShrink: 0 }}
                  onClick={() => onArm(cam.id)}>
                  {placingId === cam.id ? 'Click the plan…' : 'Place'}
                </button>
              </div>
            ))}
          </div>
        )}
        <label style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 5 }}>
          Add camera to this map…
        </label>
        <select value="" onChange={e => { if (e.target.value) onAdd(e.target.value); }} style={{ width: '100%' }}>
          <option value="">Select a camera…</option>
          {unassigned.map(cam => <option key={cam.id} value={cam.id}>{cam.name}</option>)}
        </select>
      </div>
    </div>
  );
}

/** Edit-mode tray for HA peripherals — the inventory minus what's already
 *  pinned to this plan. */
export function HaTray({ placedCount, unplaced, placingHaId, onArm }: {
  placedCount: number;
  unplaced: Device[];
  placingHaId: string | null;
  onArm: (id: string) => void;
}) {
  return (
    <div className="panel" style={{ marginBottom: 0 }}>
      <div className="panel-head">
        <div className="panel-head-l"><div className="panel-title">Devices on this map</div></div>
        <div className="panel-head-r" style={{ fontSize: 11.5, color: 'var(--dim)' }}>
          {placedCount} placed
        </div>
      </div>
      <div className="panel-body">
        {unplaced.length === 0 ? (
          <div style={{ fontSize: 12, color: 'var(--dim)' }}>
            Every peripheral is placed on this plan.
          </div>
        ) : (
          <>
            <label style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>
              Add a peripheral — pick it, then click the plan
            </label>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 260, overflowY: 'auto' }}>
              {unplaced.map(d => (
                <div key={d.id} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                  <span style={{ fontSize: 12.5, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    <span style={{ color: 'var(--dim)', marginRight: 6 }}>{d.glyph}</span>
                    {d.name}
                  </span>
                  <button
                    className={placingHaId === d.id ? 'btn-primary btn-sm' : 'btn-subtle btn-sm'}
                    style={{ flexShrink: 0 }}
                    onClick={() => onArm(d.id)}>
                    {placingHaId === d.id ? 'Click the plan…' : 'Place'}
                  </button>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** HA device detail: state, bridge address and (in edit mode) remove. */
export function HaDetailPanel({ device, pins, canManage, editMode, onRemove }: {
  device: Device | null;
  pins: HaPin[];
  canManage: boolean;
  editMode: boolean;
  onRemove: (id: string) => void;
}) {
  return (
    <div className="panel" style={{ marginBottom: 0 }}>
      <div className="panel-head">
        <div className="panel-head-l"><div className="panel-title">HA device</div></div>
      </div>
      <div className="panel-body">
        {!device ? (
          <div style={{ fontSize: 12.5, color: 'var(--dim)', textAlign: 'center', padding: '20px 0' }}>
            {pins.length === 0
              ? (canManage
                  ? 'No peripherals placed on this plan yet — turn on Edit placement to add them.'
                  : 'No peripherals placed on this plan yet.')
              : 'Click a device pin on the map to see its detail here.'}
          </div>
        ) : (
          <>
            <HaControlPanel device={device} />
            {editMode && pins.some(p => p.id === device.id) && (
              <button className="btn-danger btn-sm" style={{ width: '100%', marginTop: 10 }}
                onClick={() => onRemove(device.id)}>
                Remove from map
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

/** Camera detail: snapshot, status, GPS (editable in edit mode) and the deep
 *  links into Live / Playback. */
export function CameraDetailPanel({
  camera, snapUrl, motionState, autoPlaced, editMode,
  onSaveGps, onCopyGps, onOpenLive, onOpenPlayback, onRemove,
}: {
  camera: Camera | null;
  snapUrl: string | null;
  motionState?: string;
  autoPlaced: boolean;
  editMode: boolean;
  onSaveGps: (cam: Camera, value: string) => void;
  onCopyGps: (text: string) => void;
  onOpenLive: () => void;
  onOpenPlayback: (slug: string) => void;
  onRemove: (cam: Camera) => void;
}) {
  return (
    <div className="panel" style={{ marginBottom: 0 }}>
      <div className="panel-head">
        <div className="panel-head-l"><div className="panel-title">Camera detail</div></div>
      </div>
      <div className="panel-body">
        {!camera ? (
          <div style={{ fontSize: 12.5, color: 'var(--dim)', textAlign: 'center', padding: '20px 0' }}>
            Click a dot on the map to see its detail here.
          </div>
        ) : (
          <>
            {snapUrl
              ? <img src={snapUrl} style={{ width: '100%', borderRadius: 'var(--r-md)', display: 'block', marginBottom: 10, background: '#0A0E13' }} />
              : <div className="thumb-placeholder" style={{ width: '100%', height: 140, marginBottom: 10 }}>No snapshot</div>}
            <div style={{ fontSize: 14, fontWeight: 650, marginBottom: 4 }}>{camera.name}</div>
            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>{zoneOf(camera)}</div>
            <div style={{ marginBottom: 14 }}>
              <span className={`badge ${STATUS_BADGE[camera.health_status] || 'badge-gray'}`}>
                {camera.health_status}
              </span>
              {motionState === 'TRIGGERED' && (
                <span className="badge badge-red badge-live" style={{ marginLeft: 6 }}><span className="badge-dot" />Motion</span>
              )}
            </div>
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 3 }}>GPS coordinates</div>
              {editMode ? (
                <GpsEditor initial={(camera.metadata?.gps as string) || ''}
                  onSave={v => onSaveGps(camera, v)} />
              ) : (() => {
                const g = parseGps(camera.metadata?.gps as string | undefined);
                const raw = (camera.metadata?.gps as string) || '';
                return (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ fontSize: 12.5, color: g ? 'var(--text)' : 'var(--dim)' }}>
                      {g ? `${g.lat}, ${g.lng}` : (raw || 'not set')}
                    </span>
                    {g && (
                      <button className="btn-subtle btn-sm" title="Copy coordinates"
                        onClick={() => onCopyGps(`${g.lat}, ${g.lng}`)}>⧉</button>
                    )}
                  </div>
                );
              })()}
              {autoPlaced && (
                <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 3 }}>
                  Auto-placed from GPS{editMode ? ' — drag the dot to pin it manually' : ''}
                </div>
              )}
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <button className="btn-ghost btn-sm" onClick={onOpenLive}>Open in Live</button>
              <button className="btn-ghost btn-sm" onClick={() => onOpenPlayback(camera.slug)}>Open in Playback</button>
              {editMode && (
                <button className="btn-danger btn-sm" onClick={() => onRemove(camera)}>
                  Remove from map
                </button>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
