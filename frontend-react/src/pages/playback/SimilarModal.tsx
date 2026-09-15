/**
 * SimilarModal.tsx — "find similar" results, shown over playback.
 *
 * Deliberately a THIN reuse of Smart Search's pieces (ResultCard, the clip
 * preview, the withheld summary): search-by-example is the same search with a
 * different query type, and giving it its own card/score/preview components is
 * how the two would drift apart. What differs is only the destination — a hit
 * opened from here jumps THIS playback session (via onOpenAt → PlaybackPage's
 * keyed remount) instead of navigating from another page.
 */
import { useState } from 'react';

import { Modal } from '@/components/Modal';
import {
  summariseWithheld, type PersonHit, type SearchCamera, type SearchResponse,
} from '@/lib/smartsearch';
import { EventClipModal } from '@/pages/smartsearch/EventClipModal';
import { ResultCard } from '@/pages/smartsearch/ResultCard';
import { WEAK_TOP_SCORE } from '@/pages/smartsearch/searchUi';

export function SimilarModal({ outcome, onClose, onOpenAt }: {
  outcome: SearchResponse<PersonHit>;
  onClose: () => void;
  /** Jump this playback session to (camera slug, epoch seconds). */
  onOpenAt: (slug: string, epochSec: number) => void;
}) {
  const [eventClip, setEventClip] = useState<{ cam: SearchCamera; whenMs: number } | null>(null);
  const withheld = summariseWithheld(outcome.withheld || {});
  const results = outcome.results || [];
  const topScore = results.reduce((m, r) => Math.max(m, r.score), 0);

  return (
    <>
      {eventClip && (
        <EventClipModal cam={eventClip.cam} whenMs={eventClip.whenMs}
                        onClose={() => setEventClip(null)} />
      )}
      <Modal open title="Similar people" width={860} onClose={onClose}>
        <div className="ss-resulthead" style={{ marginBottom: 10 }}>
          <b>{results.length}</b> {results.length === 1 ? 'match' : 'matches'}, best first
          {withheld.total > 0 && (
            <span className="ss-note"
                  title="Playback can only open cameras in this registry that the recorder holds footage for.">
              {' '}· {withheld.total} withheld ({withheld.text})
            </span>
          )}
        </div>
        {results.length > 0 && topScore < WEAK_TOP_SCORE && (
          <div className="ss-note" style={{ margin: '0 0 10px' }}>
            ⚠ Weak matches — the index always returns its nearest crops, so these
            may just be the closest of an unrelated set.
          </div>
        )}
        {results.length === 0 ? (
          <div className="empty" style={{ padding: '24px 0' }}>
            No similar people on cameras this VMS records.
            {withheld.total > 0 && ` The index matched ${outcome.matched}, all withheld — ${withheld.text}.`}
          </div>
        ) : (
          <div className="ss-grid" style={{ maxHeight: '60vh', overflowY: 'auto' }}>
            {results.map(h => (
              <ResultCard
                key={h.id}
                title="Person"
                subtitle={h.tracker_id != null ? `track ${h.tracker_id}` : null}
                when={h.when_ms}
                sensor={h.sensor_id}
                camera={h.camera}
                score={h.score}
                facts={[
                  ['Detector conf.', h.confidence != null ? `${(h.confidence * 100).toFixed(1)}%` : null],
                ]}
                onOpen={() => { onClose(); onOpenAt(h.camera.slug, h.when_ms / 1000); }}
                onShowEvent={() => setEventClip({ cam: h.camera, whenMs: h.when_ms })}
                shot={{ domain: 'person', id: h.id, bbox: h.bbox, hasFrame: h.has_frame }}
              />
            ))}
          </div>
        )}
      </Modal>
    </>
  );
}
