/**
 * EventsTab — the AI activity events raised by each camera's configured
 * activities, live.
 *
 *   1. Live events   the latest ten across every camera, newest on top
 *   2. Camera cards  one per camera with AI analytics configured: what it is
 *                    configured for, and its recent events across all of them
 *   3. Entry / Exit  at the very bottom: one camera's tripwire crossings, Entry
 *                    and Exit counted per time bucket, as a graph
 *   4. Camera view   one camera's events in full, filtered by activity and time
 *                    (?cam=<slug>, so it deep-links and Back works)
 *
 * Everything here is read from /api/analytics/events. The events themselves are
 * decided by the analytics pipeline's activity logic and stored by the API; this
 * page derives none of them. Open Playback plays the event's clip in place, in
 * the same popup Smart Search uses (NVR /clip), covering the event from its
 * pre-roll to its post-roll — the operator never leaves this page. An Entry /
 * Exit crossing has no clip: its rows say which way it went, and it is counted
 * on the graph.
 */
import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '@/lib/auth';
import { eventClip, type AiEvent, type EventClip } from '@/lib/aiEvents';
import { EventClipModal } from '@/pages/smartsearch/EventClipModal';
import { CameraEventCard } from './events/CameraEventCard';
import { CameraEventsView } from './events/CameraEventsView';
import { EntryExitAnalytics } from './events/EntryExitAnalytics';
import { LiveEventTicker } from './events/LiveEventTicker';
import { useEventsOverview } from './events/useEventsOverview';

export function EventsTab({ onOpenConfig }: { onOpenConfig: () => void }) {
  const [params, setParams] = useSearchParams();
  const { me } = useAuth();
  const perms: Record<string, boolean> = me?.permissions || {};
  const canPlayback = perms.playback_search !== false;
  const { data, error, fresh } = useEventsOverview();

  const openSlug = params.get('cam');
  const setOpen = (slug: string | null) => {
    const next = new URLSearchParams(params);
    if (slug) next.set('cam', slug); else next.delete('cam');
    setParams(next);
  };
  // Open Playback plays the event's clip right here, in the popup Smart Search
  // uses — the operator never leaves the Events page.
  const [clip, setClip] = useState<EventClip | null>(null);
  const openPlayback = (ev: AiEvent) => setClip(eventClip(ev));
  const clipModal = clip && (
    <EventClipModal cam={clip.cam} whenMs={clip.whenMs} before={clip.before}
                    after={clip.after} label={clip.label} onClose={() => setClip(null)} />
  );

  if (error?.kind === 'denied') {
    return (
      <div className="emptystate" style={{ marginTop: 'var(--s4)' }}>
        <div className="glyph">⊘</div>
        <h4>AI events are not available to your role</h4>
        <p>Ask an administrator to grant your role the AI Analytics permission.</p>
      </div>
    );
  }

  if (openSlug) {
    const card = data?.cameras.find(c => c.camera.slug === openSlug);
    if (!data) return <div className="skel" style={{ height: 240, marginTop: 'var(--s4)' }} />;
    if (!card) {
      return (
        <div className="emptystate" style={{ marginTop: 'var(--s4)' }}>
          <div className="glyph">◇</div>
          <h4>No AI analytics on this camera</h4>
          <p>“{openSlug}” has no activities configured, or is no longer registered.</p>
          <button className="btn-ghost btn-sm" onClick={() => setOpen(null)}>← All cameras</button>
        </div>
      );
    }
    return (
      <>
        <CameraEventsView key={card.camera.slug} card={card} canPlayback={canPlayback}
                          onOpenPlayback={openPlayback} onBack={() => setOpen(null)} />
        {clipModal}
      </>
    );
  }

  const unavailable = error?.kind === 'unreachable' ? error.message : null;

  return (
    <div className="fade">
      <div className="page-head">
        <div className="page-head-l">
          <p className="lede">
            Events raised by the activities configured on each camera, newest first and updating live.
            What each camera watches for is set under <b>Configuration</b>.
          </p>
        </div>
      </div>

      <LiveEventTicker events={data ? data.latest : unavailable ? [] : null} fresh={fresh}
                       canPlayback={canPlayback} onOpenPlayback={openPlayback}
                       unavailable={unavailable} />

      <div className="aev-section-head">
        <div>
          <div className="aev-section-title">Cameras</div>
          <div className="panel-sub">
            {data ? `${data.cameras.length} camera${data.cameras.length === 1 ? '' : 's'} with AI analytics configured` : 'Loading…'}
          </div>
        </div>
      </div>

      {data == null ? (
        <div className="aev-grid">
          {[0, 1].map(i => <div key={i} className="skel" style={{ height: 180 }} />)}
        </div>
      ) : !data.cameras.length ? (
        <div className="emptystate">
          <div className="glyph">◇</div>
          <h4>No camera has AI analytics configured</h4>
          <p>Add an activity to a camera under Cameras → Configuration → AI Config, and its events will appear here.</p>
          <button className="btn-ghost btn-sm" onClick={onOpenConfig}>Open Configuration</button>
        </div>
      ) : (
        <div className="aev-grid">
          {data.cameras.map(card => (
            <CameraEventCard key={card.camera.slug} card={card} fresh={fresh} canPlayback={canPlayback}
                             onOpenPlayback={openPlayback} onOpen={() => setOpen(card.camera.slug)} />
          ))}
        </div>
      )}

      <EntryExitAnalytics cards={data ? data.cameras : null} onOpenConfig={onOpenConfig} />
      {clipModal}
    </div>
  );
}
