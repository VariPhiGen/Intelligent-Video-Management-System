/**
 * MapOverlays.tsx — the chrome that floats ON the plan but sits OUTSIDE its
 * pan/zoom transform: zoom buttons, the edit-mode hint, and the heatmap's
 * window picker and empty state.
 *
 * Each stops mouse events from reaching the surface underneath, so clicking a
 * control never starts a pan or drops a placement.
 */
import { HEATMAP_WINDOWS, HeatScale } from './MockLayers';
import type { View } from './mapConstants';

/** Zoom in / out / reset and the name-label toggle. Zoom was wheel-only and
 *  reset was a double-click — both invisible; these make the gestures
 *  discoverable without changing them. */
export function ZoomControls({ view, labels, onZoomIn, onZoomOut, onReset, onToggleLabels }: {
  view: View;
  labels: boolean;
  onZoomIn: () => void;
  onZoomOut: () => void;
  onReset: () => void;
  onToggleLabels: () => void;
}) {
  const buttons: [string, string, () => void, boolean][] = [
    ['＋', 'Zoom in', onZoomIn, true],
    ['−', 'Zoom out', onZoomOut, true],
    ['⟲', 'Reset view (or double-click the plan)', onReset, true],
    ['A', labels
      ? (view === 'ha' ? 'Hide device names' : 'Hide camera names')
      : (view === 'ha' ? 'Show device names' : 'Show camera names'),
      onToggleLabels, labels],
  ];
  return (
    <div style={{ position: 'absolute', right: 12, top: 12, zIndex: 3, display: 'flex', flexDirection: 'column', gap: 4 }}
      onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}>
      {buttons.map(([glyph, tip, fn, on], i) => (
        <button key={i} className={on ? 'btn-ghost btn-sm' : 'btn-subtle btn-sm'} title={tip}
          style={{ width: 28, height: 28, padding: 0, background: on ? 'var(--surface)' : undefined }}
          onClick={fn}>{glyph}</button>
      ))}
    </div>
  );
}

/** What edit mode is waiting for: a click to place the armed marker, or a drag
 *  to reposition an existing one. */
export function EditHint({ armedName, isPlacing, view }: {
  armedName: string | null;
  isPlacing: boolean;
  view: View;
}) {
  const noun = view === 'ha' ? 'pin' : 'dot';
  return (
    <div style={{
      position: 'absolute', left: '50%', top: 12, transform: 'translateX(-50%)', zIndex: 3,
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--r-md)', padding: '5px 12px', fontSize: 11.5,
      color: isPlacing ? 'var(--accent)' : 'var(--muted)',
      fontWeight: isPlacing ? 600 : 400,
      boxShadow: '0 1px 4px rgba(0,0,0,.15)', pointerEvents: 'none', maxWidth: '70%',
    }}>
      {isPlacing
        ? `Click the plan to place ${armedName ?? (view === 'ha' ? 'this device' : 'this camera')}`
        : `Drag a ${noun} to reposition — every drop saves on its own`}
    </div>
  );
}

/** Heatmap caption: which span is counted, how much of it lands on this plan,
 *  and what the blob intensities mean. */
export function HeatCaption({ heatWindow, onWindow, eventsOnMap }: {
  heatWindow: string;
  onWindow: (key: string) => void;
  eventsOnMap: number;
}) {
  return (
    <div style={{
      position: 'absolute', left: 12, top: 12, zIndex: 3,
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--r-md)', padding: '6px 10px', fontSize: 11, color: 'var(--muted)',
      display: 'flex', flexDirection: 'column', gap: 6,
    }} onMouseDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span style={{ marginRight: 4 }}>Motion in the last</span>
        {HEATMAP_WINDOWS.map(w => (
          <button key={w.key}
            className={heatWindow === w.key ? 'btn-primary btn-sm' : 'btn-subtle btn-sm'}
            style={{ padding: '1px 8px', fontSize: 11 }}
            onClick={() => onWindow(w.key)}>{w.label}</button>
        ))}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <span>{eventsOnMap} event{eventsOnMap === 1 ? '' : 's'} on this plan</span>
        <HeatScale />
      </div>
    </div>
  );
}

/** A blank plan reads as "nothing is wrong" when it actually means "nothing was
 *  measured". Four different reasons, and the fix differs for each — say which
 *  one this is. */
export function HeatEmpty({ dotCount, totalEvents, eventsInWindow }: {
  dotCount: number;
  totalEvents: number;
  eventsInWindow: number;
}) {
  return (
    <div style={{
      position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%,-50%)',
      zIndex: 2, pointerEvents: 'none', textAlign: 'center', maxWidth: '70%',
      background: 'var(--surface)', border: '1px solid var(--border)',
      borderRadius: 'var(--r-md)', padding: '10px 16px',
      boxShadow: '0 2px 8px rgba(0,0,0,.2)',
    }}>
      <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 3 }}>
        No motion recorded in this window
      </div>
      <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>
        {dotCount === 0
          ? 'No cameras are placed on this plan yet.'
          : totalEvents === 0
            ? 'The motion service has no events for any camera yet.'
            : eventsInWindow > 0
              ? `${eventsInWindow} event${eventsInWindow === 1 ? '' : 's'} in this window, all from cameras that aren't on this plan.`
              : `${totalEvents} event${totalEvents === 1 ? '' : 's'} outside this window — try a longer span.`}
      </div>
    </div>
  );
}
