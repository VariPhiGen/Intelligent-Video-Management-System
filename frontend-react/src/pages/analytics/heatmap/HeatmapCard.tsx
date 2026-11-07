/**
 * HeatmapCard — one camera's heatmap for today (00:00 to now, from the stored
 * 5-minute blocks), its state, and the switch that stops or starts it.
 *
 * The metadata is rendered HERE, not burnt into the picture: the artifact is
 * only the camera frame under its heat, so the same bytes stay readable when
 * the period, the status or the camera's name change around them.
 */
import { cardState, type HeatmapCamera } from '@/lib/heatmaps';
import { HeatmapImage } from './HeatmapImage';

const LABEL: Record<string, string> = {
  on: 'Heatmap: ON', frozen: 'Heatmap: OFF (frozen)', off: 'Heatmap: OFF',
  idle: 'Heatmap: waiting for its schedule',
  waiting: 'Heatmap: waiting for the first overlay',
};

export function fmt(when: string | null | undefined): string {
  if (!when) return '—';
  const d = new Date(when);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

export function fmtTime(when: string | null | undefined): string {
  if (!when) return '—';
  const d = new Date(when);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleTimeString();
}

/** "Mon–Fri 08:00 → 18:00", or what the operator actually configured. */
export function scheduleText(cam: HeatmapCamera): string {
  const s = cam.schedule;
  if (!s || (!s.active_days && !s.active_hours)) return 'Every day, all day';
  const days = s.active_days?.length ? s.active_days.join(', ') : 'Every day';
  const hours = s.active_hours ? `${s.active_hours.start} → ${s.active_hours.end}` : 'all day';
  return `${days}, ${hours}`;
}

export function HeatmapCard({ cam, busy, onToggle, onFullscreen, onSelect, selected }: {
  cam: HeatmapCamera;
  busy: boolean;
  onToggle: (cam: HeatmapCamera) => void;
  onFullscreen: (cam: HeatmapCamera) => void;
  /** Show this camera in the range view at the top of the page. */
  onSelect?: (cam: HeatmapCamera) => void;
  selected?: boolean;
}) {
  const state = cardState(cam);
  const frozen = state === 'frozen';
  const art = cam.latest_artifact;

  return (
    <div className={'panel hm-card' + (selected ? ' hm-card-selected' : '')}>
      <div className="panel-head">
        <div className="panel-head-l">
          <div className="panel-title">{cam.name}</div>
          <div className="panel-sub">{scheduleText(cam)}</div>
        </div>
        <span className={'badge hm-badge hm-' + state}>{LABEL[state]}</span>
      </div>

      <div className={'panel-body flush' + (onSelect ? ' hm-pick' : '')}
           onClick={onSelect ? () => onSelect(cam) : undefined}
           title={onSelect ? 'Show this camera above' : undefined}>
        <HeatmapImage cameraId={cam.camera_id} name={cam.name} dimmed={frozen}
                      alt={`Heatmap overlay for ${cam.name}`} />
      </div>

      <div className="hm-meta">
        <div><span className="hm-k">Heatmap period</span><span className="hm-v">Today, 00:00 → now</span></div>
        <div>
          <span className="hm-k">{frozen ? 'Frozen at' : 'Last updated'}</span>
          <span className="hm-v">{fmt(art?.captured_at ?? null)}</span>
        </div>
        {!cam.camera_enabled && (
          <div className="hm-note">This camera is disabled, so nothing is being added.</div>
        )}
        {cam.camera_enabled && state === 'idle' && (
          <div className="hm-note">Outside its schedule — it will start again in its next window.</div>
        )}
        {cam.camera_enabled && state === 'waiting' && (
          <div className="hm-note">
            In its window, but no overlay has arrived yet — the camera has not
            delivered frames to the analytics pipeline.
          </div>
        )}
      </div>

      <div className="panel-actions hm-actions">
        <button className={cam.runtime_enabled ? 'btn-secondary' : 'btn-primary'}
                disabled={busy} onClick={() => onToggle(cam)}>
          {cam.runtime_enabled ? 'Turn Off' : 'Turn On'}
        </button>
        <button className="btn-ghost" onClick={() => onFullscreen(cam)}>
          Fullscreen
        </button>
      </div>
    </div>
  );
}
