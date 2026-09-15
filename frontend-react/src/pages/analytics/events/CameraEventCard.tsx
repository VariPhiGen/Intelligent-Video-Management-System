/**
 * CameraEventCard — one camera's AI events at a glance: which activities it is
 * configured for, and its most recent events across all of them.
 *
 * A configured camera with no events keeps its card and says so. "Configured
 * and quiet" and "not configured" are different facts, and a card that
 * disappeared would make them look the same.
 */
import { isEntryExit, type AiEvent, type CameraEventsCard } from '@/lib/aiEvents';
import { ActivityChip, CrossingLabel } from './LiveEventTicker';
import { fmtEventTime, newestFirst } from './eventsModel';

export function CameraEventCard({ card, fresh, canPlayback, onOpenPlayback, onOpen }: {
  card: CameraEventsCard;
  fresh: Set<string>;
  canPlayback: boolean;
  onOpenPlayback: (ev: AiEvent) => void;
  onOpen: () => void;
}) {
  const { camera, activities } = card;
  const events = newestFirst(card.events);

  return (
    <section className="panel aev-card" aria-label={`${camera.name} AI events`}>
      <div className="panel-head">
        <div className="panel-head-l" style={{ minWidth: 0 }}>
          <div style={{ minWidth: 0 }}>
            <button className="aev-card-title" onClick={onOpen}>{camera.name}</button>
            <div className="panel-sub aev-mono">{camera.slug}</div>
          </div>
        </div>
        <div className="panel-actions">
          {!camera.enabled && <span className="badge badge-gray">Disabled</span>}
          <button className="btn-ghost btn-sm" onClick={onOpen}>View all →</button>
        </div>
      </div>
      <div className="panel-body aev-card-body">
        <div className="aev-configured">
          <span className="aev-label">Configured</span>
          <div className="aev-chips">
            {activities.map(a => <ActivityChip key={a.key} label={a.label} color={a.color} />)}
          </div>
        </div>
        <div className="aev-label" style={{ marginTop: 'var(--s3)' }}>Recent events</div>
        {events.length ? (
          <ul className="aev-card-list">
            {events.map(ev => {
              const { time, date } = fmtEventTime(ev.started_at);
              return (
                <li key={ev.id} data-event-id={ev.id} className={`aev-card-row${fresh.has(ev.id) ? ' aev-fresh' : ''}`}>
                  <span className="aev-time">{time}{date && <span className="aev-date">{date}</span>}</span>
                  <span className="aev-card-activity">{ev.activity.label}</span>
                  {isEntryExit(ev) ? <CrossingLabel ev={ev} /> : canPlayback && (
                    <button className="btn-ghost btn-sm aev-play" onClick={() => onOpenPlayback(ev)}>
                      ▶ Open Playback
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="aev-none">No events recorded</div>
        )}
      </div>
    </section>
  );
}
