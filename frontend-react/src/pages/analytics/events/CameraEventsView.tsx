/**
 * CameraEventsView — one camera's events in detail, with the filters the event
 * model actually supports: activity, and a from/to range on when the event
 * started. Newest first; older pages load on demand.
 *
 * While the range is open-ended (no "To"), the newest page is re-read on a poll
 * and merged over what is already loaded, so new events appear at the top
 * without throwing away the older pages the operator scrolled to.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError } from '@/lib/api';
import {
  fetchEvents, isEntryExit, localInputToIso, type AiEvent, type CameraEventsCard, type EventQuery,
} from '@/lib/aiEvents';
import { ActivityChip, CrossingLabel } from './LiveEventTicker';
import { fmtEventDuration, fmtEventTime, mergePages } from './eventsModel';

export const PAGE_SIZE = 50;
export const VIEW_POLL_MS = 10_000;

export function CameraEventsView({ card, canPlayback, onOpenPlayback, onBack }: {
  card: CameraEventsCard;
  canPlayback: boolean;
  onOpenPlayback: (ev: AiEvent) => void;
  onBack: () => void;
}) {
  const { camera, activities } = card;
  const [activity, setActivity] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [events, setEvents] = useState<AiEvent[] | null>(null);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const fromIso = localInputToIso(from);
  const toIso = localInputToIso(to);
  const rangeError = fromIso && toIso && fromIso > toIso ? '"From" is after "To".' : null;

  const query = useMemo<EventQuery>(() => ({
    camera: camera.slug, activity: activity || undefined, from: fromIso, to: toIso, limit: PAGE_SIZE,
  }), [camera.slug, activity, fromIso, toIso]);

  // Every filter change starts a new list; a response for an older filter is dropped.
  const generation = useRef(0);

  const loadFirst = useCallback(async (merge: boolean) => {
    const gen = generation.current;
    try {
      const page = await fetchEvents(query);
      if (gen !== generation.current) return;
      setEvents(prev => (merge && prev ? mergePages(page.events, prev) : page.events));
      if (!merge) setNextBefore(page.next_before);
      setErr(null);
    } catch (e: unknown) {
      if (gen !== generation.current) return;
      setErr(e instanceof ApiError ? e.message : 'Events could not be loaded.');
      if (!merge) setEvents([]);
    }
  }, [query]);

  useEffect(() => {
    if (rangeError) return;
    generation.current += 1;
    setEvents(null);
    setNextBefore(null);
    loadFirst(false);
    if (toIso) return;                              // a closed range cannot grow
    const t = setInterval(() => loadFirst(true), VIEW_POLL_MS);
    return () => clearInterval(t);
  }, [loadFirst, rangeError, toIso]);

  async function loadOlder() {
    if (!nextBefore) return;
    const gen = generation.current;
    setLoadingMore(true);
    try {
      const page = await fetchEvents({ ...query, before: nextBefore });
      if (gen !== generation.current) return;
      setEvents(prev => mergePages(prev || [], page.events));
      setNextBefore(page.next_before);
    } catch (e: unknown) {
      if (gen === generation.current) setErr(e instanceof ApiError ? e.message : 'Older events could not be loaded.');
    } finally {
      if (gen === generation.current) setLoadingMore(false);
    }
  }

  const clear = () => { setActivity(''); setFrom(''); setTo(''); };
  const filtered = !!(activity || from || to);
  const showZone = !!events?.some(e => e.zone);
  // The last column plays a clip, or — for an Entry / Exit crossing — says which way it went.
  const showAction = canPlayback || !!events?.some(isEntryExit);

  return (
    <div className="fade">
      <div className="page-head">
        <div className="page-head-l">
          <button className="btn-ghost btn-sm" onClick={onBack} style={{ marginBottom: 'var(--s3)' }}>← All cameras</button>
          <h2 className="aev-view-title">{camera.name} <span className="aev-mono aev-dim">{camera.slug}</span></h2>
          <div className="aev-configured" style={{ marginTop: 'var(--s2)' }}>
            <span className="aev-label">Configured</span>
            <div className="aev-chips">
              {activities.map(a => <ActivityChip key={a.key} label={a.label} color={a.color} />)}
            </div>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div className="panel-head-l">
            <div>
              <div className="panel-title">Events</div>
              <div className="panel-sub">Newest first{toIso ? '' : ' · updating live'}.</div>
            </div>
          </div>
        </div>
        <div className="panel-body" style={{ paddingBottom: 0 }}>
          <div className="ss-filters aev-filters" role="group" aria-label="Event filters">
            <label className="ss-field">
              <span className="ss-field-label">Activity</span>
              <select value={activity} onChange={e => setActivity(e.target.value)} aria-label="Activity">
                <option value="">All activities</option>
                {activities.map(a => <option key={a.key} value={a.key}>{a.label}</option>)}
              </select>
            </label>
            <label className="ss-field">
              <span className="ss-field-label">From</span>
              <input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} aria-label="From" />
            </label>
            <label className="ss-field">
              <span className="ss-field-label">To</span>
              <input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} aria-label="To" />
            </label>
            {filtered && (
              <div className="ss-field" style={{ justifyContent: 'flex-end' }}>
                <button className="btn-ghost btn-sm" onClick={clear}>Clear filters</button>
              </div>
            )}
          </div>
          {(rangeError || err) && <div className="aev-error" role="alert">{rangeError || err}</div>}
        </div>
        <div className="panel-body flush" style={{ marginTop: 'var(--s4)' }}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 130 }}>Time</th>
                  <th>Activity</th>
                  {showZone && <th>Zone</th>}
                  <th style={{ width: 110 }}>Duration</th>
                  {showAction && <th style={{ width: 150 }} />}
                </tr>
              </thead>
              <tbody>
                {events == null && !rangeError && [0, 1, 2].map(i => (
                  <tr key={i}><td colSpan={5}><span className="skel" style={{ width: '50%' }} /></td></tr>
                ))}
                {events != null && !events.length && (
                  <tr><td colSpan={5} style={{ padding: 0 }}>
                    <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                      <div className="glyph">◇</div>
                      <h4>No events recorded</h4>
                      <p>{filtered ? 'Nothing matches these filters.' : 'This camera\'s configured activities have not raised an event yet.'}</p>
                    </div>
                  </td></tr>
                )}
                {events?.map(ev => {
                  const { time, date } = fmtEventTime(ev.started_at);
                  return (
                    <tr key={ev.id} data-event-id={ev.id}>
                      <td>
                        <div className="aev-mono" style={{ fontSize: 12.5 }}>{time}</div>
                        {date && <div className="aev-dim" style={{ fontSize: 10.5 }}>{date}</div>}
                      </td>
                      <td><ActivityChip label={ev.activity.label} color={ev.activity.color} /></td>
                      {showZone && <td>{ev.zone || <span className="aev-dim">Whole frame</span>}</td>}
                      <td>
                        {ev.ended_at
                          ? <span className="aev-mono">{fmtEventDuration(ev.duration_s)}</span>
                          : <span className="aev-dim">—</span>}
                      </td>
                      {showAction && (
                        <td style={{ textAlign: 'right' }}>
                          {isEntryExit(ev) ? <CrossingLabel ev={ev} /> : canPlayback && (
                            <button className="btn-ghost btn-sm" onClick={() => onOpenPlayback(ev)}>▶ Open Playback</button>
                          )}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
        {nextBefore && (
          <div className="panel-foot" style={{ display: 'flex', justifyContent: 'center' }}>
            <button className="btn-ghost btn-sm" onClick={loadOlder} disabled={loadingMore}>
              {loadingMore ? 'Loading…' : 'Load older events'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
