/**
 * MockLayers.tsx — the Map tab's companion layers: HA device pins (mock-fed
 * off the peripherals demo data), the motion heatmap (real counts off
 * `/motion/events`), and the shared legend. Everything here renders INSIDE
 * the same transformed surface div MapPage already pans/zooms — these
 * components only produce the absolutely-positioned children, they don't
 * own the transform.
 *
 * HA *placement* is real — pins sit where an operator dropped them, stored on
 * the sitemap row (`ha_devices`, migration 028) and passed in as `pins`. HA
 * *control* is still a preview: `HaControlPanel` flips local component state
 * only — nothing here talks to a device. That's honest given there's no
 * Home-Assistant bridge wired up yet (the same demo boundary
 * `peripheralsData.ts` draws for the Peripherals pages), and the device list
 * itself is that module's demo data.
 */
import { Fragment, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  toneColor, type Device, type DeviceCategory,
} from '../peripherals/peripheralsData';
import type { MapDot } from './mapHelpers';

/** Shape of `GET /motion/events` entries — `camera` is the registry slug.
 *  Mirrors the established local MotionEvent in EventsTab.tsx. */
export interface MotionEvent {
  id: number;
  camera: string;
  started_at: string;
  ended_at: string | null;
}

/** One HA peripheral pinned to a plan — the shape stored in
 *  `sitemaps.ha_devices` and handed back by the API. */
export interface HaPin {
  id: string;
  x: number;
  y: number;
}

// ── HA devices layer ─────────────────────────────────────────────────────

/** Square outline pins (the "HA peripheral" marker) — renders instead of
 * camera dots when `view === 'ha'`. Only *placed* devices appear: `pins` is
 * the sitemap's stored placement, joined here against the live device list so
 * a pin for a device that has since disappeared is skipped rather than drawn
 * nameless. `scale` counter-scales the pin so it stays a constant screen size
 * while the surface zooms, same as the camera dots.
 *
 * In edit mode the pointer handlers are the caller's — MapPage owns the
 * drag-vs-click slop and the coordinate maths, exactly as it does for camera
 * dots, so both layers behave identically under the same pan/zoom. */
export function HaLayer({
  devices, pins, selectedId, onSelect, scale = 1, labels = true,
  editMode = false, onPinPointerDown, onPinPointerMove, onPinPointerUp,
}: {
  devices: Device[];
  pins: HaPin[];
  selectedId?: string | null;
  onSelect: (id: string) => void;
  scale?: number;
  labels?: boolean;
  editMode?: boolean;
  onPinPointerDown?: (e: React.PointerEvent, id: string) => void;
  onPinPointerMove?: (e: React.PointerEvent, id: string) => void;
  onPinPointerUp?: (e: React.PointerEvent, id: string) => void;
}) {
  const byId = useMemo(() => new Map(devices.map(d => [d.id, d])), [devices]);
  return (
    <>
      {pins.map(p => {
        const d = byId.get(p.id);
        if (!d) return null;
        const col = toneColor(d.tone);
        const isSelected = d.id === selectedId;
        return (
          <Fragment key={d.id}>
            {labels && (
              <span style={{
                position: 'absolute', left: `${p.x * 100}%`, top: `${p.y * 100}%`,
                transform: `translate(12px, -50%) scale(${1 / scale})`, transformOrigin: '0 50%',
                pointerEvents: 'none', whiteSpace: 'nowrap',
                fontSize: 11, fontWeight: 600, lineHeight: 1.5,
                color: 'var(--text)', background: 'var(--surface)',
                border: '1px solid var(--border)', borderRadius: 4, padding: '0 5px',
                boxShadow: '0 1px 3px rgba(0,0,0,.18)', opacity: .92,
              }}>{d.name}</span>
            )}
            <button
              title={`${d.name} · ${d.category} · ${d.state}${editMode ? ' — drag to reposition' : ''}`}
              onMouseDown={e => e.stopPropagation()}
              onClick={e => { e.stopPropagation(); onSelect(d.id); }}
              onPointerDown={e => onPinPointerDown?.(e, d.id)}
              onPointerMove={e => onPinPointerMove?.(e, d.id)}
              onPointerUp={e => onPinPointerUp?.(e, d.id)}
              style={{
                position: 'absolute', left: `${p.x * 100}%`, top: `${p.y * 100}%`,
                transform: `translate(-50%,-50%) scale(${1 / scale})`,
                width: 16, height: 16, borderRadius: 4, padding: 0,
                border: `2px solid ${col}`, background: 'transparent',
                boxShadow: isSelected ? '0 0 0 2px var(--accent)' : 'none',
                cursor: editMode ? 'grab' : 'pointer', touchAction: 'none',
              }} />
          </Fragment>
        );
      })}
    </>
  );
}

// ── HA control panel (right panel, view === 'ha') ────────────────────────

const KIND_LABEL: Record<DeviceCategory, string> = {
  'Lighting': 'Toggle light',
  'Access control': 'Lock / Unlock',
  'Audio · Sensors': 'Activate siren',
};

/** Name, kind, what the device reports, and the action a bridge would perform.
 *
 * The action button is DISABLED and says why. It used to flip local component
 * state — clicking "Lock" turned the chip to LOCKED and nothing else happened,
 * which reads as control on a page full of live camera data. There is no
 * Home-Assistant bridge, so the honest surface is a disabled button with the
 * reason attached, and a state chip that says UNKNOWN until something
 * reports one. */
export function HaControlPanel({ device }: { device: Device }) {
  const nav = useNavigate();
  const col = toneColor(device.tone);
  const actionLabel = KIND_LABEL[device.category];
  const known = !!device.lastState;

  return (
    <>
      <div style={{ fontSize: 14, fontWeight: 650, marginBottom: 4 }}>{device.name}</div>
      <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 8 }}>
        {device.category} · {device.location}
      </div>
      <div style={{ marginBottom: 14 }}>
        <span className="badge" style={{
          background: `color-mix(in srgb, ${col} 14%, transparent)`, color: col,
        }}>
          {device.state}
        </span>
        {!known && (
          <span style={{ fontSize: 11, color: 'var(--dim)', marginLeft: 8 }}>
            nothing has reported this device
          </span>
        )}
      </div>
      {device.externalId && (
        <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 10, wordBreak: 'break-all' }}>
          Bridge address: <code>{device.externalId}</code>
        </div>
      )}
      <button className="btn btn-sm" style={{ width: '100%', marginBottom: 10 }} disabled
        title="No Home-Assistant bridge is configured — nothing can switch this device yet">
        {actionLabel}
      </button>
      <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 14, lineHeight: 1.5 }}>
        Control needs the Home-Assistant bridge. Placement and inventory are live;
        switching a device is not.
      </div>
      <button className="btn-primary btn-sm" style={{ width: '100%' }} onClick={() => nav('/peripherals')}>
        Open peripherals →
      </button>
    </>
  );
}

// ── heatmap window ───────────────────────────────────────────────────────

/** Selectable spans for the heatmap. `hours: null` = every event the feed
 * returned. 24h is the default: a day is the span an operator actually reads
 * a floor by, and "recent events" (the old caption) told them nothing about
 * what they were looking at. */
export const HEATMAP_WINDOWS: { key: string; label: string; hours: number | null }[] = [
  { key: '1h', label: '1h', hours: 1 },
  { key: '24h', label: '24h', hours: 24 },
  { key: '7d', label: '7d', hours: 24 * 7 },
  { key: 'all', label: 'All', hours: null },
];

/** Events inside `hours` of now. Unparseable timestamps are dropped rather
 * than counted — a NaN date would otherwise slip past every comparison and
 * inflate whichever window happened to be selected. */
export function eventsInWindow(events: MotionEvent[], hours: number | null): MotionEvent[] {
  if (hours == null) return events;
  const cutoff = Date.now() - hours * 3600_000;
  return events.filter(e => {
    const t = Date.parse(e.started_at);
    return Number.isFinite(t) && t >= cutoff;
  });
}

/** The blob gradient as a horizontal bar — without it the blobs are just
 * red smudges with no stated meaning. Sits with the other map captions. */
export function HeatScale() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--muted)' }}>
      <span>less</span>
      <span style={{
        width: 64, height: 8, borderRadius: 4, display: 'inline-block',
        background: 'linear-gradient(90deg, color-mix(in srgb, var(--red) 25%, transparent), var(--red))',
      }} />
      <span>more</span>
    </div>
  );
}

// ── heatmap layer ────────────────────────────────────────────────────────

/** Blurred radial-gradient blobs under each camera dot, sized by that
 * camera's share of the busiest camera's event count. Purely visual —
 * `pointerEvents: none` so the (separately rendered) camera dots stay
 * clickable on top.
 *
 * Normalization is per-map: `max` is derived only from the slugs of the
 * placed `dots`, not from the whole event feed — a busy camera on another
 * sitemap must not flatten this map's blobs. The busiest camera ON THIS MAP
 * always renders at full intensity. */
export function HeatmapLayer({ dots, events }: { dots: MapDot[]; events: MotionEvent[] }) {
  const counts = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of events) m.set(e.camera, (m.get(e.camera) || 0) + 1);
    return m;
  }, [events]);
  const max = useMemo(
    () => Math.max(1, ...dots.map(d => counts.get(d.cam.slug) || 0)),
    [counts, dots],
  );

  return (
    <>
      {dots.map(d => {
        const count = counts.get(d.cam.slug) || 0;
        if (count <= 0) return null;
        const share = count / max;
        const size = 60 + 140 * share;
        return (
          <div key={d.cam.id} style={{
            position: 'absolute', left: `${d.x * 100}%`, top: `${d.y * 100}%`,
            transform: 'translate(-50%,-50%)',
            width: size, height: size, borderRadius: '50%', pointerEvents: 'none',
            background: `radial-gradient(circle, color-mix(in srgb, var(--red) ${Math.round(25 + 45 * share)}%, transparent) 0%, transparent 70%)`,
            filter: 'blur(6px)',
          }} />
        );
      })}
    </>
  );
}

// ── legend ────────────────────────────────────────────────────────────────

function LegendRow({ shape, color, label }: { shape: 'dot' | 'square'; color: string; label: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5, color: 'var(--text2)' }}>
      {shape === 'dot'
        ? <span style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0 }} />
        : <span style={{ width: 9, height: 9, borderRadius: 2, border: `1.5px solid ${color}`, background: 'transparent', flexShrink: 0 }} />}
      {label}
    </div>
  );
}

/** Bottom-left legend covering all four map symbols — sits OUTSIDE the pan/
 * zoom transform (a fixed corner of the surface container), same as the
 * caption chips. */
export function Legend() {
  return (
    <div style={{ margin: '8px 2px 0', color: 'var(--muted)' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px 18px' }}>
        <LegendRow shape="dot" color="var(--green)" label="Camera connected" />
        <LegendRow shape="dot" color="var(--yellow)" label="Health issue" />
        <LegendRow shape="dot" color="var(--orange, #e8963c)" label="Active alarm" />
        {/* Placement is real; only the device's state is unavailable. The old
            "(preview)" label now understates what the pin means. */}
        <LegendRow shape="square" color="var(--muted)" label="HA peripheral (no live state)" />
      </div>
    </div>
  );
}
