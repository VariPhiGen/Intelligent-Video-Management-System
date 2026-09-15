/**
 * SmartSearchPage — natural-language search over the CLIP index built by the
 * GPU appliance (people from the `person_crops` Kafka topic, vehicles from the
 * edge ANPR ingest).
 *
 * Read-only: this page searches and links out. Nothing here writes to the index.
 *
 * On snapshots — the index stores an `image_url`, but it points at MinIO on the
 * GPU host, which an operator's browser cannot reach. So a hit does not try to
 * render a remote still; it carries camera + timestamp and hands off to the NVR
 * playback this VMS already owns, which is the authoritative footage anyway.
 *
 * Scoping lives in the backend (`routers/search.py`), not here. Every hit that
 * reaches this component resolves to a camera in this registry with footage
 * covering that instant, so the action is always live and there is no such
 * thing as an unopenable card. All this page does with the rest is *report* it:
 * the header says how many the index matched and why the difference was
 * withheld.
 *
 * The parts: useSmartSearch.ts holds the state and the query, SearchFilters
 * the form, ResultCard a hit, EventClipModal the popup, searchUi the shared
 * score bands. This file is the layout and the empty states.
 */
import { useCallback, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';

import type { SearchCamera } from '@/lib/smartsearch';

import { EventClipModal } from './EventClipModal';
import { ResultCard } from './ResultCard';
import { AnprPanel } from './AnprPanel';
import { FacesPanel } from './FacesPanel';
import { SearchFilters } from './SearchFilters';
import { trackerSuffix } from './trackerId';
import { WEAK_TOP_SCORE, tabCount } from './searchUi';
import { useSmartSearch } from './useSmartSearch';

const PAGE_SIZE = 12;

export function SmartSearchPage() {
  const nav = useNavigate();
  const s = useSmartSearch();
  const { mode, outcome, withheld, stats, busy, err, reachable, configured, recorderDown } = s;

  /** Hand off to the NVR playback already in this VMS, seeked to the hit. */
  const openInPlayback = useCallback((cam: SearchCamera, whenMs: number) => {
    nav(`/playback?cam=${encodeURIComponent(cam.slug)}&t=${Math.floor(whenMs / 1000)}`);
  }, [nav]);

  /** Event-clip popup: null when closed, else the hit to preview (~20s). */
  const [eventClip, setEventClip] = useState<{ cam: SearchCamera; whenMs: number } | null>(null);

  // ANPR is deep-linkable, because the AI-detections dashboard lists plates it
  // has read and clicking one has to land somewhere that answers "where else
  // has this been?". Read once on mount; a tab click owns it afterwards.
  const [sp] = useSearchParams();
  const seededPlate = sp.get('plate') || '';
  // ONE view variable, not a boolean per extra view. With `anpr` as a flag, a
  // second non-search view meant every `!anpr` in this file silently started
  // meaning "search OR faces" — and the tab bar's own comment below records
  // that two tabs reading as active at once is a bug that already happened
  // once here.
  type View = 'search' | 'anpr' | 'faces';
  const [view, setView] = useState<View>(
    sp.get('mode') === 'anpr' || seededPlate ? 'anpr'
      : sp.get('mode') === 'faces' ? 'faces' : 'search');
  const hasSearched = outcome !== null;
  const count = outcome?.results.length ?? 0;
  // The selected camera, if the operator has excluded it from indexing. Drives
  // the one empty state that must not say "no matches": a camera nobody indexed
  // returns nothing for every query, which is not a fact about the footage.
  const selectedUnindexed = s.camSlug
    ? s.unindexed.find(c => c.slug === s.camSlug) ?? null
    : null;
  const domainStats = stats[mode];

  const cards = useMemo(() => {
    if (mode === 'people') {
      return (s.people?.results || []).map(h => (
        <ResultCard
          key={h.id}
          title="Person"
          when={h.when_ms}
          sensor={h.sensor_id}
          camera={h.camera}
          score={h.score}
          facts={[
            // Without the camera prefix the line above already prints — see
            // trackerId.ts. Hover gives the complete id.
            ['Tracker', h.tracker_id ? trackerSuffix(String(h.tracker_id), h.sensor_id ?? undefined) : null],
            ['Detector conf.', h.confidence != null ? `${(h.confidence * 100).toFixed(1)}%` : null],
            // How many observations of this object the index folded into this
            // one result. Shown only when it stands for more than itself.
            ['Sightings', h.sightings && h.sightings > 1 ? `${h.sightings} observations` : null],
            ['Frame', h.frame_number != null ? String(h.frame_number) : null],
            ['Stream', h.pad_index != null ? String(h.pad_index) : null],
          ]}
          onOpen={() => openInPlayback(h.camera, h.when_ms)}
          onShowEvent={() => setEventClip({ cam: h.camera, whenMs: h.when_ms })}
          shot={{ domain: 'person', id: h.id, bbox: h.bbox, hasFrame: h.has_frame }}
        />
      ));
    }
    return (s.vehicles?.results || []).map(h => (
      <ResultCard
        key={h.id}
        title={h.plate || 'Vehicle'}
        subtitle={h.plate ? null : 'plate not read'}
        when={h.when_ms}
        sensor={h.sensor_id}
        camera={h.camera}
        score={h.score}
        facts={[
          ['Tracker', h.tracker_id ? trackerSuffix(String(h.tracker_id), h.sensor_id ?? undefined) : null],
          // The DETECTOR's confidence in the class below, exactly as the person
          // card reports its own. It was labelled "Plate conf." and printed on a
          // 0-100 scale, so a vehicle the detector was 39% sure of read as "0%"
          // — and the plate's real confidence is a different column entirely.
          ['Detector conf.', h.confidence != null ? `${(h.confidence * 100).toFixed(1)}%` : null],
          ['Type', h.vehicle_type && h.vehicle_type !== 'unknown' ? h.vehicle_type : null],
          ['Colour', h.color && h.color !== 'unknown' ? h.color : null],
          ['Brand', h.brand && h.brand !== 'unknown' ? h.brand : null],
          ['Sightings', h.sightings && h.sightings > 1 ? `${h.sightings} observations` : null],
          ['Speed', h.vehicle_speed || null],
          ['Violation', h.violation || null],
        ]}
        onOpen={() => openInPlayback(h.camera, h.when_ms)}
        onShowEvent={() => setEventClip({ cam: h.camera, whenMs: h.when_ms })}
        shot={{ domain: 'vehicles', id: h.id, bbox: h.bbox, hasFrame: h.has_frame }}
      />
    ));
  }, [mode, s.people, s.vehicles, openInPlayback]);

  // Client-side pagination over the fetched result set. Only the current page's
  // cards mount, so only their frames load.
  const pageCount = Math.max(1, Math.ceil(cards.length / PAGE_SIZE));
  const safePage = Math.min(s.page, pageCount - 1);
  const pagedCards = cards.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  return (
    <div className="fade">
      {eventClip && (
        <EventClipModal
          cam={eventClip.cam}
          whenMs={eventClip.whenMs}
          onClose={() => setEventClip(null)}
        />
      )}
      <div className="page-head">
        <div className="page-head-l">
          {/* Describes similarity search, which ANPR is not — it has its own
              lede, and two contradictory ones stacked would be worse than none. */}
          <p className="lede">
            {view === 'faces'
              ? <>Upload a photo — or up to 5 of the same person — to find other appearances of that <b>face</b>. This searches
                  faces recorded by this VMS — it does not identify anyone.</>
              : view === 'anpr'
              /* What the screen is for and what it covers — nothing about what
                 happens after you search, which is the idle prompt's job. The
                 two used to say the same three sentences. */
              ? <>Find every sighting of a vehicle by its <b>number plate</b>. Only cameras
                  this VMS records are searched.</>
              : <>Describe what you're looking for in plain language. Matches are ranked by
                  visual similarity against the analytics index, then handed to <b>Playback</b> for
                  the footage. Only cameras this VMS records are searched — every result opens.</>}
          </p>
        </div>
      </div>

      <div className="tabs page-tabs">
        {/* `!anpr &&` on both: ANPR is a third view, not a filter, but `mode`
            keeps its last value while it is open — so without this the tab it
            was opened from stays lit and two tabs read as active at once. */}
        <div className={`tab${view === 'search' && mode === 'people' ? ' active' : ''}`} onClick={() => { setView('search'); s.switchMode('people'); }}>
          People{tabCount(stats.people)}
        </div>
        <div className={`tab${view === 'search' && mode === 'vehicles' ? ' active' : ''}`} onClick={() => { setView('search'); s.switchMode('vehicles'); }}>
          Vehicles{tabCount(stats.vehicles)}
        </div>
        {/* ANPR is a lookup, not a ranked search, so it owns its own view
            rather than adding a filter to Vehicles. No count badge: a plate
            index size says nothing useful before you have a plate to look up. */}
        <div className={`tab${view === 'anpr' ? ' active' : ''}`} onClick={() => setView('anpr')}>
          ANPR
        </div>
        {/* Faces takes a photograph, not text — SFace has no text encoder — so
            it is a view of its own rather than a filter on People. No count
            badge for ANPR's reason inverted: the number here is small by
            nature (a usable face appears on a few percent of person passes)
            and a small count beside a big one reads as a fault. */}
        <div className={`tab${view === 'faces' ? ' active' : ''}`} onClick={() => setView('faces')}>
          Faces
        </div>
      </div>

      {configured === false && (
        <div className="panel ss-offline">
          <div className="panel-body">
            <b>Smart Search is not configured.</b>
            <div className="hint">
              This installation has no analytics index deployed, so there is nothing to
              search. Recording, playback and analytics are unaffected.
            </div>
          </div>
        </div>
      )}

      {reachable === false && configured !== false && (
        <div className="panel ss-offline">
          <div className="panel-body">
            <b>Search index unavailable.</b>
            <div className="hint">
              This VMS could not reach the analytics index. Search is unavailable until it
              responds; recording and playback are unaffected.
            </div>
          </div>
        </div>
      )}

      {recorderDown && (
        <div className="panel ss-offline">
          <div className="panel-body">
            <b>Recorder inventory unavailable.</b>
            <div className="hint">
              Results can be checked against this camera registry but not against retained
              footage, so a match may open on a gap in Playback.
            </div>
          </div>
        </div>
      )}

      {view === 'anpr' && <AnprPanel initialPlate={seededPlate} />}

      {view === 'faces' && <FacesPanel cameras={s.camOptions} />}

      {view === 'search' && <SearchFilters
        mode={mode}
        query={s.query} setQuery={s.setQuery} run={s.run} busy={busy}
        camSlug={s.camSlug} setCamSlug={s.setCamSlug} camOptions={s.camOptions}
        timeFrom={s.timeFrom} setTimeFrom={s.setTimeFrom}
        timeTo={s.timeTo} setTimeTo={s.setTimeTo}
        plate={s.plate} setPlate={s.setPlate}
        vehType={s.vehType} setVehType={s.setVehType}
        colour={s.colour} setColour={s.setColour}
        topK={s.topK} setTopK={s.setTopK}
        sortBy={s.sortBy} setSortBy={s.setSortBy}
        sortDir={s.sortDir} setSortDir={s.setSortDir}
        threshold={s.threshold} setThreshold={s.setThreshold}
      />}

      {view === 'search' && err && <div className="panel row-err ss-error"><div className="panel-body">{err}</div></div>}

      {view === 'search' && busy && <div className="ss-loading"><span className="spinner" /> Searching the index…</div>}

      {/* Everything matched belongs to footage this VMS does not hold — a
          different problem from "nothing matched", and a different fix. */}
      {view === 'search' && !busy && hasSearched && count === 0 && withheld.total > 0 && !err && (
        <div className="emptystate">
          <div className="empty">
            <b>No matches on cameras this VMS records.</b>
            <div className="hint">
              The index matched {outcome?.matched}{' '}
              {outcome?.matched === 1 ? 'result' : 'results'}, all withheld — {withheld.text}.
              {' '}Search is limited to cameras in this registry with footage on the recorder,
              because those are the only ones Playback can open.
            </div>
          </div>
        </div>
      )}

      {/* An opted-out camera returns nothing for every query. Saying "no
          matches" there would be a lie of omission: it reads as "this person
          was never here" when the truth is that nobody ever looked. */}
      {view === 'search' && !busy && hasSearched && count === 0 && !err && selectedUnindexed && (
        <div className="emptystate">
          <div className="empty">
            <b>This camera is not indexed for Smart Search.</b>
            <div className="hint">
              <b>{selectedUnindexed.name || selectedUnindexed.slug}</b> is excluded from
              indexing, so no query can match it — this is not a statement about what
              the footage contains. Recording and Playback are unaffected. An
              administrator can re-enable indexing in Cameras → AI.
            </div>
          </div>
        </div>
      )}

      {view === 'search' && !busy && hasSearched && count === 0 && withheld.total === 0 && !err && !selectedUnindexed && (
        <div className="emptystate">
          <div className="empty">
            <b>No matches.</b>
            <div className="hint">
              Try a broader description, lower the minimum score, or widen the time range.
              {domainStats?.vectors_count === 0
                ? ` Nothing is indexed yet for your cameras, so no ${mode} query can match.`
                : domainStats?.vectors_count
                  ? ` The ${mode} index holds ${domainStats.vectors_count.toLocaleString()} entries for your cameras.`
                  : ''}
              {!s.camSlug && s.unindexed.length > 0 && (
                <>
                  {' '}
                  <b>
                    {s.unindexed.length} camera{s.unindexed.length === 1 ? ' is' : 's are'} excluded
                    from indexing
                  </b>{' '}
                  ({s.unindexed.map(c => c.name || c.slug).join(', ')}), so this search never
                  covered {s.unindexed.length === 1 ? 'it' : 'them'}.
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {view === 'search' && !busy && count > 0 && (
        <>
          <div className="ss-resulthead">
            <b>{count}</b> {count === 1 ? 'match' : 'matches'}, best first
            {outcome?.truncated && <span className="ss-note"> · more available — raise “Results”</span>}
            {withheld.total > 0 && (
              <span
                className="ss-note"
                title="Playback can only open cameras in this registry that the recorder holds footage for."
              >
                {' '}· {withheld.total} withheld ({withheld.text})
              </span>
            )}
          </div>
          {s.topScore < WEAK_TOP_SCORE && (
            <div className="ss-note" style={{
              margin: '2px 0 10px', padding: '7px 10px', borderRadius: 6,
              color: 'var(--yellow)', background: 'var(--yellowSoft)',
              border: '1px solid color-mix(in srgb, var(--yellow) 24%, transparent)',
            }}>
              ⚠ Weak match — the best result scores {s.topScore.toFixed(3)}. The index
              always returns its nearest crops, so these may simply be the closest
              of an unrelated set rather than anything that matches the description.
              Descriptions of appearance score highest; names and identifiers score
              like noise, because the index holds no names.
            </div>
          )}
          <div className="ss-grid">{pagedCards}</div>
          {pageCount > 1 && (
            <div style={{ display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'center', marginTop: 14 }}>
              <button className="btn-secondary btn-sm" disabled={safePage === 0} onClick={() => s.setPage(safePage - 1)}>← Prev</button>
              <span className="ss-note">Page {safePage + 1} of {pageCount}</span>
              <button className="btn-secondary btn-sm" disabled={safePage >= pageCount - 1} onClick={() => s.setPage(safePage + 1)}>Next →</button>
            </div>
          )}
        </>
      )}

      {view === 'search' && !hasSearched && !busy && reachable !== false && (
        <div className="emptystate">
          <div className="empty">
            <b>Search the analytics index.</b>
            <div className="hint">
              People and vehicles are indexed continuously from the detection pipeline.
              Describe what you are looking for. To find a specific number plate, use the
              ANPR tab — that is an exact lookup, not a description.
            </div>
          </div>
        </div>
      )}

    </div>
  );
}
