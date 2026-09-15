/**
 * EntryExitAnalytics — the last section of the Events page: how many people
 * entered and left through one camera's tripwires over a chosen range.
 *
 * Every number is counted by the API from the stored Entry and Exit records
 * (GET /api/analytics/events/entry-exit): per time bucket for the graph, and the
 * totals as the sum of those buckets, so the graph and its totals never
 * disagree. Only cameras with Entry / Exit configured are offered. The graph
 * follows the clock, re-read on a poll that pauses while the tab is hidden.
 */
import { useEffect, useState } from 'react';
import { ApiError } from '@/lib/api';
import {
  DEFAULT_ENTRY_EXIT_MINUTES, ENTRY_EXIT_ACTIVITY, ENTRY_EXIT_RANGES, fetchEntryExit,
  type CameraEventsCard, type EntryExitStats,
} from '@/lib/aiEvents';
import { bucketWords, EntryExitChart, SERIES } from './EntryExitChart';

export const ENTRY_EXIT_POLL_MS = 15_000;

/** "hour", "5 minutes", "24 hours" — as in "the last …". */
function spanWords(minutes: number): string {
  if (minutes < 60) return `${minutes} minutes`;
  return minutes === 60 ? 'hour' : `${minutes / 60} hours`;
}

export function EntryExitAnalytics({ cards, onOpenConfig }: {
  /** The overview's camera cards; null while the first response is outstanding. */
  cards: CameraEventsCard[] | null;
  onOpenConfig?: () => void;
}) {
  const cameras = (cards ?? [])
    .filter(c => c.activities.some(a => a.key === ENTRY_EXIT_ACTIVITY))
    .map(c => c.camera);
  const [picked, setPicked] = useState<string | null>(null);
  const slug = cameras.find(c => c.slug === picked)?.slug ?? cameras[0]?.slug ?? null;
  const [minutes, setMinutes] = useState(DEFAULT_ENTRY_EXIT_MINUTES);
  const [tripwire, setTripwire] = useState('');
  const [stats, setStats] = useState<EntryExitStats | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!slug) return;
    const cam = slug;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => { if (alive) timer = setTimeout(load, ENTRY_EXIT_POLL_MS); };

    async function load() {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        schedule();
        return;
      }
      try {
        const s = await fetchEntryExit(cam, minutes, tripwire || null);
        if (!alive) return;
        setStats(s);
        setErr(null);
      } catch (e: unknown) {
        if (!alive) return;
        // The tripwire was removed from the camera since it was picked: count them all.
        if (e instanceof ApiError && e.status === 404 && tripwire) { setTripwire(''); return; }
        setErr(e instanceof ApiError && e.status === 403
          ? 'Entry / Exit analytics are not available to your role.'
          : e instanceof Error ? e.message : String(e));
        if (e instanceof ApiError && e.status === 403) return;
      }
      schedule();
    }

    load();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [slug, minutes, tripwire]);

  // What is on screen until the answer for the current choice arrives.
  const stale = !stats || stats.camera.slug !== slug || stats.minutes !== minutes
    || (stats.tripwire ?? '') !== tripwire;
  const wires = stats && stats.camera.slug === slug ? stats.tripwires : [];
  const unoriented = wires.filter(t => !t.oriented);

  return (
    <section className="panel ee" aria-labelledby="ee-title">
      <div className="panel-head">
        <div className="panel-head-l">
          <div>
            <h3 className="ee-title" id="ee-title">Entry / Exit analytics</h3>
            <div className="panel-sub">People crossing a camera's tripwires, counted from the stored Entry and Exit records.</div>
          </div>
        </div>
        {slug && (
          <div className="panel-actions">
            <span className="badge badge-green badge-live"><span className="badge-dot" />Live</span>
          </div>
        )}
      </div>

      <div className="panel-body">
        {cards == null ? (
          <div className="skel" style={{ height: 280 }} />
        ) : !slug ? (
          <div className="emptystate" style={{ border: 'none' }}>
            <div className="glyph">⟂</div>
            <h4>No camera has Entry / Exit configured</h4>
            <p>
              Draw a tripwire across a doorway under Configuration → Zones &amp; Analytics, set its entry
              direction, and add Entry / exit for it under AI Config. Its crossings are counted here.
            </p>
            {onOpenConfig && <button className="btn-ghost btn-sm" onClick={onOpenConfig}>Open Configuration</button>}
          </div>
        ) : (
          <>
            <div className="ee-controls" role="group" aria-label="Entry / Exit graph options">
              <label className="ss-field">
                <span className="ss-field-label">Camera</span>
                <select aria-label="Camera" value={slug}
                        onChange={e => { setPicked(e.target.value); setTripwire(''); }}>
                  {cameras.map(c => <option key={c.slug} value={c.slug}>{c.name}</option>)}
                </select>
              </label>
              {wires.length > 1 && (
                <label className="ss-field">
                  <span className="ss-field-label">Tripwire</span>
                  <select aria-label="Tripwire" value={tripwire} onChange={e => setTripwire(e.target.value)}>
                    <option value="">All tripwires</option>
                    {wires.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                  </select>
                </label>
              )}
              <div className="ss-field">
                <span className="ss-field-label">Duration</span>
                <div className="ch-seg" role="group" aria-label="Duration">
                  {ENTRY_EXIT_RANGES.map(r => (
                    <button key={r.minutes} className={r.minutes === minutes ? 'active' : ''}
                            aria-pressed={r.minutes === minutes} onClick={() => setMinutes(r.minutes)}>
                      {r.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="ee-summary" aria-busy={stale}>
              <div className="ee-totals">
                {SERIES.map(s => (
                  <div key={s.key} className="ee-total">
                    <i className="ee-swatch" style={{ background: s.color }} aria-hidden="true" />
                    <span>{s.label}:</span>
                    <b aria-label={`${s.label} total`}>{stats && !stale ? stats.totals[s.key] : '—'}</b>
                  </div>
                ))}
              </div>
              <span className="ee-range">
                Last {spanWords(minutes)}{stats && !stale ? ` · per ${bucketWords(stats.bucket_seconds)}` : ''}
              </span>
            </div>

            {unoriented.length > 0 && (
              <div className="ee-note" role="note">
                <span>!</span>
                <span>
                  <b>{unoriented.map(t => t.name).join(', ')}</b> {unoriented.length === 1 ? 'has' : 'have'} no
                  entry direction, so {unoriented.length === 1 ? 'its' : 'their'} crossings are not counted. Set
                  the direction arrow under Configuration → Zones &amp; Analytics.
                </span>
              </div>
            )}
            {err && <div className="aev-error" role="alert">{err}</div>}

            {stats ? (
              <div className={stale ? 'ee-stale' : undefined}>
                <EntryExitChart stats={stats} />
              </div>
            ) : !err && <div className="skel" style={{ height: 262, marginTop: 'var(--s3)' }} />}
            {stats && !stale && stats.totals.entry + stats.totals.exit === 0 && (
              <div className="ee-quiet">No crossings in the last {spanWords(minutes)}.</div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
