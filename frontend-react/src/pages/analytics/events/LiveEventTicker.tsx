/**
 * LiveEventTicker — the compact live channel at the top of the Events tab: the
 * latest AI events across every camera, newest on top, at most ten.
 *
 * A new event slides in at the top and pushes the rest down; the list is a
 * fixed short height so the card stays landscape-shaped and the camera cards
 * below it never move as events arrive. No thumbnails — an operator acts from
 * here by opening playback. An Entry / Exit crossing has no clip; its row says
 * which way the person went instead.
 */
import { crossingDirection, isEntryExit, type AiEvent } from '@/lib/aiEvents';
import { fmtEventTime, tickerItems } from './eventsModel';

export function ActivityChip({ label, color }: { label: string; color: string }) {
  return (
    <span className="aev-chip" title={label}>
      <span className="aev-chip-dot" style={{ background: color }} />
      {label}
    </span>
  );
}

/** Which way an Entry / Exit event crossed its tripwire, shown where other events
 *  offer Open Playback. Touches recorded before crossings had a direction say so. */
export function CrossingLabel({ ev }: { ev: AiEvent }) {
  const direction = crossingDirection(ev);
  if (!direction) {
    return (
      <span className="aev-cross aev-cross-none"
            title="Recorded before crossings had a direction — not counted as Entry or Exit">
        No direction
      </span>
    );
  }
  return (
    <span className="aev-cross" data-direction={direction}>
      <i className="ee-swatch" aria-hidden="true"
         style={{ background: direction === 'entry' ? 'var(--ee-entry)' : 'var(--ee-exit)' }} />
      {direction === 'entry' ? 'Entry' : 'Exit'}
    </span>
  );
}

export function LiveEventTicker({ events, fresh, canPlayback, onOpenPlayback, unavailable }: {
  /** null while the first response is outstanding. */
  events: AiEvent[] | null;
  fresh: Set<string>;
  canPlayback: boolean;
  onOpenPlayback: (ev: AiEvent) => void;
  /** Set when the feed could not be read, so an empty list is not mistaken for a quiet site. */
  unavailable?: string | null;
}) {
  const items = events ? tickerItems(events) : [];

  return (
    <div className="panel aev-ticker">
      <div className="panel-head">
        <div className="panel-head-l">
          <div>
            <div className="panel-title">Live events</div>
            <div className="panel-sub">Latest {items.length || ''} AI events across all cameras, newest first.</div>
          </div>
        </div>
        <div className="panel-actions">
          {unavailable
            ? <span className="badge badge-yellow"><span className="badge-dot" />Reconnecting</span>
            : <span className="badge badge-green badge-live"><span className="badge-dot" />Live</span>}
        </div>
      </div>
      <div className="panel-body flush">
        {events == null ? (
          <div className="aev-ticker-list">
            {[0, 1, 2].map(i => <div key={i} className="aev-row"><span className="skel" style={{ width: '60%' }} /></div>)}
          </div>
        ) : !items.length ? (
          <div className="aev-ticker-empty">
            {unavailable
              ? 'Events are unavailable right now — the list refills when the API answers again.'
              : 'No AI events yet. They appear here the moment a configured activity fires.'}
          </div>
        ) : (
          <ol className="aev-ticker-list" aria-label="Latest AI events">
            {items.map(ev => {
              const { time, date } = fmtEventTime(ev.started_at);
              return (
                <li key={ev.id} data-event-id={ev.id} className={`aev-row${fresh.has(ev.id) ? ' aev-fresh' : ''}`}>
                  <span className="aev-time">
                    {time}{date && <span className="aev-date">{date}</span>}
                  </span>
                  <span className="aev-cam" title={ev.camera.slug}>{ev.camera.name}</span>
                  <ActivityChip label={ev.activity.label} color={ev.activity.color} />
                  <span className="aev-zone">{ev.zone || ''}</span>
                  {isEntryExit(ev) ? <CrossingLabel ev={ev} /> : canPlayback && (
                    <button className="btn-ghost btn-sm aev-play" onClick={() => onOpenPlayback(ev)}>
                      ▶ Open Playback
                    </button>
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </div>
  );
}
