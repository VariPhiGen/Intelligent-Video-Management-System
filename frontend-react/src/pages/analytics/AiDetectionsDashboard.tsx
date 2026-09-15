/**
 * AiDetectionsDashboard — AI-detections overview for Analytics → Events.
 *
 * Surfaces the person/vehicle detections that feed Smart Search (from the CLIP
 * index via /api/search/detections) as an events dashboard: stat tiles, hourly
 * volume, person/vehicle breakdown, plate reads and top cameras. Read-only;
 * hand-rolled SVG/CSS (no chart lib).
 *
 * The recent-detections feed that used to close this page lives on the Smart
 * Search page now (smartsearch/RecentDetections.tsx), as whole frames with the
 * object boxed: that is where an operator goes to find someone, and a feed of
 * who was just seen belongs beside the box they would describe them in.
 *
 * PLATES ARE THE THIRD DOMAIN, AND THEY ARE NOT A THIRD SLICE. People and
 * vehicles partition the detection total; a plate is an attribute of a vehicle
 * that may or may not have been readable. So the split bar stays two-way and
 * plates get their own reading — coverage of vehicles seen — which is also the
 * number that says whether ANPR is working at all. Putting plates in the split
 * would double-count every vehicle whose plate was read.
 *
 * The page used to report plates only as a tally. A count answers "is the
 * reader running"; it does not answer "who came through", and the only way to
 * see an actual plate was to already know it and use the ANPR lookup. The
 * plates panel closes that: recent reads, and the plates seen most often.
 *
 * Presentation comes from the design system — `Stat`, `.panel`, `.emptystate`
 * and the `.aid-*` block in global.css — rather than inline styles, so the page
 * follows the theme, the viewport and pointer state like every other page. It
 * previously carried its own dark-only palette and was unreadable in light mode.
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiFetch } from '@/lib/api';
import { Stat } from './Stat';

interface PlateRead {
  id: string;
  plate: string;
  /** Mean character probability. Null on rows written before migration 003,
   *  which is not the same as a low-confidence read and must not render as 0. */
  confidence: number | null;
  camera_id: string;
  vehicle_type: string;
  timestamp: number;
}
interface TopPlate {
  plate: string;
  count: number;
  cameras: number;
  first_seen: number;
  last_seen: number;
}
interface Summary {
  total: number;
  by_domain: Record<string, number>;
  by_type: Record<string, number>;
  top_cameras: { camera: string; count: number }[];
  by_hour: { hour: string; count: number }[];
  /** Plate reads over the same window. A third count, not a slice of vehicles:
   *  a vehicle is indexed whether or not its plate could be read. */
  plates_read?: number;
  distinct_plates?: number;
  /** Denominator for the read rate. "41 plates" cannot be told apart from a
   *  broken localiser; "41 of 380 vehicles" can. */
  vehicles_seen?: number;
}
interface DetResp {
  summary: Summary;
  /** Always true — the API refuses an unscoped feed rather than showing one,
   *  since the index is shared and its busiest camera may be another site's. */
  scoped: boolean;
  /** Cameras these figures cover: registered here and known to the recorder. */
  cameras: number;
  /** False = the index cannot read plates at all, so a plate count of zero says
   *  nothing about traffic. Null = it did not report. */
  plates_active?: boolean | null;
  /** The reads themselves, newest first. Separate from `recent`: a vehicle
   *  appears there whether or not its plate was read, so folding the two would
   *  make "no plate" and "not a vehicle" look like the same row. */
  recent_plates?: PlateRead[];
  /** Ranked by sightings, not recency — one plate seen nine times is the thing
   *  worth surfacing, and it is invisible in a feed ordered by time. */
  top_plates?: TopPlate[];
}

const RANGES: Array<[string, number]> = [
  ['Today', 1],
  ['Last 7 days', 7],
  ['Last 30 days', 30],
];

function BreakdownBar({ people, vehicles }: { people: number; vehicles: number }) {
  const total = people + vehicles;
  // Nothing to divide. The old `|| 1` fallback made 0 ÷ 1 = 0% people, and the
  // remainder rule then handed the whole bar to vehicles — a full bar reading
  // "Vehicles 100%" when there were no vehicles at all.
  if (total === 0) {
    return (
      <div>
        <div className="aid-split-empty" />
        <div className="aid-none" style={{ marginTop: 'var(--s3)' }}>No detections to split.</div>
      </div>
    );
  }
  const pPct = Math.round((people / total) * 100);
  const vPct = 100 - pPct;
  return (
    <div>
      <div className="aid-split-bar" role="img"
           aria-label={`People ${pPct}%, vehicles ${vPct}%`}>
        <i className="people" style={{ width: `${pPct}%` }} />
        <i className="vehicles" style={{ width: `${vPct}%` }} />
      </div>
      <div className="aid-legend">
        <span className="people"><i className="dot" />People {pPct}%</span>
        <span className="vehicles"><i className="dot" />Vehicles {vPct}%</span>
      </div>
    </div>
  );
}

/** How many of the vehicles seen had a readable plate.
 *
 *  The honest third dimension. Plates do not partition the detection total —
 *  every plate belongs to a vehicle already counted — so this is a COVERAGE
 *  reading rather than a slice of the split bar above it.
 *
 *  It is also the number that answers the question a bare tally cannot: a low
 *  rate means the localiser is missing plates or the camera angle is wrong,
 *  and "190 plates" on its own looks identical whether the rate is 10% or 90%.
 */
function PlateCoverage({ read, vehicles, distinct }: {
  read: number; vehicles: number; distinct: number;
}) {
  // No vehicles is not 0% coverage — there was nothing to read. Saying "0%"
  // would point an operator at the plate reader for a quiet gate.
  if (vehicles === 0) {
    return <div className="aid-none" style={{ marginTop: 'var(--s3)' }}>
      No vehicles seen in this range, so there was nothing to read a plate from.
    </div>;
  }
  const pct = Math.round((read / vehicles) * 100);
  return (
    <div className="aid-cover">
      <div className="aid-cover-head">
        <b>Plates read</b>
        <span>{read.toLocaleString()} of {vehicles.toLocaleString()} vehicles</span>
      </div>
      <div className="aid-cam-track" role="img"
           aria-label={`Plates read on ${pct}% of vehicles seen`}>
        <div className="aid-cover-fill" style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
      <div className="aid-cover-sub">
        {pct}% read rate{distinct ? ` · ${distinct.toLocaleString()} distinct plate${distinct === 1 ? '' : 's'}` : ''}
      </div>
    </div>
  );
}

function whenShort(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

/** Plate reads, as numbers an operator can act on rather than a tally.
 *
 *  Every plate is a link into the ANPR lookup, because the question that
 *  follows seeing one is always "where else has this been?" — and answering it
 *  used to require retyping the plate into a different page.
 *
 *  Reads fail by NEAR MISS here, not by absence: this appliance has recorded
 *  `WB67C1277`, `WB67C127` and `WB67C118` off the same truck. That is why the
 *  confidence is shown per read and why the plate is set in a mono face — two
 *  plates one character apart have to be distinguishable at a glance.
 */
function PlatesPanel({ active, recent, top, onOpen }: {
  active: boolean | null | undefined;
  recent: PlateRead[];
  top: TopPlate[];
  onOpen: (plate: string) => void;
}) {
  // Not enabled at all. A zero here is not a statement about traffic, and
  // "no plates read" would be read as one.
  if (active === false) {
    return (
      <section className="panel">
        <div className="panel-head"><div className="panel-head-l">
          <div className="panel-title">Number plates</div>
        </div></div>
        <div className="panel-body">
          <div className="aid-none">
            <b style={{ color: 'var(--text)' }}>Plate reading is not enabled on this deployment.</b>
            <div style={{ marginTop: 'var(--s2)' }}>
              No plate has been read, so nothing here would be a statement about
              which vehicles passed. It needs plate-detection weights on the index
              service (<code>SEARCH_PLATE_WEIGHTS</code>). People and vehicle
              detection are unaffected.
            </div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-head"><div className="panel-head-l">
        <div className="panel-title">Number plates</div>
        <div className="panel-sub">select a plate to see every sighting of it</div>
      </div></div>
      <div className="panel-body">
        {recent.length === 0 ? (
          // Enabled, but nothing read in THIS range — a different fact from the
          // one above, and it has a different fix.
          <div className="aid-none">
            No plate was read in this range. Vehicles are still indexed and
            searchable; only the plate on them is missing.
          </div>
        ) : (
          <div className="aid-plates">
            <div>
              <div className="aid-sub-title">Most recent</div>
              <div className="aid-plate-list">
                {recent.map(p => (
                  <button key={p.id} className="aid-plate" onClick={() => onOpen(p.plate)}
                          title={`Look up ${p.plate}`}>
                    <span className="aid-plate-no">{p.plate}</span>
                    <span className="aid-plate-meta">
                      {p.camera_id} · {whenShort(p.timestamp)}
                    </span>
                    {/* Null is "not recorded" (rows predating migration 003),
                        not "scored zero" — an em dash, never a number. */}
                    <span className="aid-plate-conf">
                      {p.confidence == null ? '—' : `${Math.round(p.confidence * 100)}%`}
                    </span>
                  </button>
                ))}
              </div>
            </div>
            <div>
              <div className="aid-sub-title">Seen most often</div>
              {top.length === 0 ? (
                <div className="aid-none">Nothing repeated in this range.</div>
              ) : (
                <div className="aid-plate-list">
                  {top.map(p => (
                    <button key={p.plate} className="aid-plate" onClick={() => onOpen(p.plate)}
                            title={`Look up ${p.plate}`}>
                      <span className="aid-plate-no">{p.plate}</span>
                      <span className="aid-plate-meta">
                        {p.cameras} camera{p.cameras === 1 ? '' : 's'} ·{' '}
                        {whenShort(p.first_seen)} → {whenShort(p.last_seen)}
                      </span>
                      <span className="aid-plate-count">×{p.count}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

/** The whole-page empty state.
 *
 *  Zeroed charts are worse than no charts: twelve 2px bars read as "a little
 *  activity", and four tiles of `0` give the emptiest page the most visual
 *  weight. When the range holds nothing, the page says so once — and says which
 *  of the two reasons it is, since the fix differs. */
function NoDetections({ cameras, days, onWiden }: {
  cameras: number; days: number; onWiden: () => void;
}) {
  const noCameras = cameras === 0;
  const range = days === 1 ? 'today' : `the last ${days} days`;
  return (
    <div className="emptystate">
      <div className="glyph" aria-hidden>
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="6" width="13" height="12" rx="2" />
          <path d="M16 10l5-3v10l-5-3" />
        </svg>
      </div>
      <h4>{noCameras ? 'No cameras to report on' : `No AI detections ${range}`}</h4>
      <p>
        {noCameras
          ? 'No camera here is both registered and known to the recorder, so nothing in the analytics index can be attributed to this VMS.'
          : `Nothing was indexed for ${cameras === 1 ? 'your camera' : `your ${cameras} cameras`} in this range. The AI pipeline may not be publishing crops for them yet.`}
      </p>
      {!noCameras && days < 30 && (
        <button className="btn-secondary btn-sm" onClick={onWiden}>Try the last 30 days</button>
      )}
    </div>
  );
}

export function AiDetectionsDashboard() {
  const nav = useNavigate();
  const [days, setDays] = useState(1);
  const [data, setData] = useState<DetResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr(null);
    const since = Date.now() - days * 86400_000;
    const tz = -new Date().getTimezoneOffset(); // IST → +330; hourly buckets in local time
    // limit=1: the feed moved to Smart Search, and this page reads only the
    // summary and the plates, which the index bounds on its own.
    apiFetch<DetResp>(`/search/detections?since_ms=${since}&limit=1&tz_offset_min=${tz}`)
      .then((d) => { if (alive) setData(d); })
      .catch((e) => { if (alive) setErr(e?.message || 'Failed to load detections'); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [days]);

  const s = data?.summary;
  const people = s?.by_domain?.person ?? 0;
  // Both spellings. The retiring index emitted "vehicle" while naming its route
  // "/vehicles"; the replacement is plural throughout. Reading only one silently
  // renders a zero next to a chart that plainly shows vehicles.
  const vehicles = s?.by_domain?.vehicles ?? s?.by_domain?.vehicle ?? 0;
  const platesRead = s?.plates_read ?? 0;
  // Vehicle classes only — `person` has its own tile and would flatten the bars.
  const vehicleTypes = Object.entries(s?.by_type ?? {})
    .filter(([t]) => t !== 'person')
    .sort((a, b) => b[1] - a[1]);
  const maxType = Math.max(1, ...vehicleTypes.map(([, n]) => n));
  const distinctPlates = s?.distinct_plates ?? 0;
  // Falls back to the by_domain vehicle count so this still reads correctly
  // against an index too old to send the denominator, rather than showing a
  // read rate of "190 of 0".
  const vehiclesSeen = s?.vehicles_seen ?? vehicles;
  const recentPlates = data?.recent_plates ?? [];
  const topPlates = data?.top_plates ?? [];
  const readRate = vehiclesSeen > 0 ? Math.round((platesRead / vehiclesSeen) * 100) : null;

  /** A plate is only ever half an answer; the other half is everywhere else it
   *  has been. Hands off to the ANPR lookup with the plate already run. */
  const openPlate = useCallback((plate: string) => {
    nav(`/smartsearch?mode=anpr&plate=${encodeURIComponent(plate)}`);
  }, [nav]);

  const topCam = s?.top_cameras?.[0];
  const maxHour = Math.max(1, ...(s?.by_hour?.map((h) => h.count) ?? [1]));
  const maxCam = Math.max(1, ...(s?.top_cameras?.map((c) => c.count) ?? [1]));

  // One empty state instead of a page of zeroes. Charts are suppressed rather
  // than drawn flat: an axis with no marks still reads as a measurement.
  //
  // Deliberately independent of `loading`: switching the range keeps showing
  // what is already on screen until the new answer arrives. Gating on `loading`
  // made the page fall through to the populated branch mid-fetch, so changing
  // range flashed the zeroed dashboard for the length of the request.
  const showEmpty = !!data && !err && (s?.total ?? 0) === 0;
  const showBoard = !!data && !err && (s?.total ?? 0) > 0;
  const firstLoad = loading && !data;
  const viewCls = `aid-view${loading && !firstLoad ? ' aid-fetching' : ''}`;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s4)' }}>
      <div className="aid-filters" role="group" aria-label="Time range">
        {RANGES.map(([lbl, d]) => (
          <button
            key={d}
            className={days === d ? 'btn-primary btn-sm' : 'btn-secondary btn-sm'}
            aria-pressed={days === d}
            onClick={() => setDays(d)}
          >
            {lbl}
          </button>
        ))}
      </div>

      {err && <div className="panel"><div className="panel-body" style={{ color: 'var(--red)' }}>{err}</div></div>}

      {firstLoad && (
        <div className="panel"><div className="panel-body aid-none" style={{ textAlign: 'center', padding: 'var(--s9)' }}>
          Loading detections…
        </div></div>
      )}

      {showEmpty && (
        <div className={viewCls}>
          <NoDetections cameras={data?.cameras ?? 0} days={days} onWiden={() => setDays(30)} />
        </div>
      )}

      {showBoard && (
      <div className={viewCls} style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s4)' }}>
        <div className="stat-grid" style={{ marginBottom: 0 }}>
          <Stat label="AI detections" value={(s?.total ?? 0).toLocaleString()}
                sub={data ? `across ${data.cameras} camera${data.cameras === 1 ? '' : 's'}` : undefined} />
          <Stat label="People" value={people.toLocaleString()} />
          <Stat label="Vehicles" value={vehicles.toLocaleString()} />
          {/* Plates are their own count. Zero when the reader is off is not a
              statement about traffic, so it renders as "—" with the reason
              rather than as a number somebody would act on. */}
          <Stat
            label="Plates read"
            value={data?.plates_active === false ? '—' : platesRead.toLocaleString()}
            sub={data?.plates_active === false
              ? 'plate reading not enabled'
              : readRate !== null
                // The rate, not just the distinct count: it is what separates
                // "the reader is working" from "the reader is missing plates",
                // and both look the same as a bare number.
                ? `${readRate}% of vehicles · ${distinctPlates.toLocaleString()} distinct`
                : undefined}
          />
          {/* A camera slug in a slot sized for numerals: at 28px with numeric
              letter-spacing, `exit-gate-hvte` set the tile's whole width and a
              longer name would simply overflow. `text` drops it to a size a
              name reads at and lets it wrap. */}
          <Stat label="Top camera" value={topCam ? topCam.camera : '—'} text
                sub={topCam ? `${topCam.count.toLocaleString()} events` : undefined} />
        </div>

        <div className="aid-split">
          <section className="panel">
            <div className="panel-head"><div className="panel-head-l">
              <div className="panel-title">Detections by hour</div>
            </div></div>
            <div className="panel-body">
              <div className="aid-hours">
                {(s?.by_hour ?? []).map((h, i) => (
                  <div key={i} className="aid-hour">
                    {/* The bar is a share of the plot area, not a pixel count, so
                        the chart grows into whatever height the row gives it —
                        which is what lets this card match its neighbour instead
                        of ending early and leaving a gap. */}
                    <div className="aid-hour-track">
                      <div className="aid-hour-bar" role="img"
                           aria-label={`${h.hour}: ${h.count} detection${h.count === 1 ? '' : 's'}`}
                           title={`${h.hour}: ${h.count}`}
                           style={{ height: `${(h.count / maxHour) * 100}%` }} />
                    </div>
                    <span className="aid-hour-lab">{h.hour.slice(0, 2)}</span>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head"><div className="panel-head-l">
              <div className="panel-title">Breakdown</div>
            </div></div>
            <div className="panel-body">
              <BreakdownBar people={people} vehicles={vehicles} />
              {/* Coverage, not a third slice — see the note at the top of this
                  file. Suppressed when the reader is off, where a rate would
                  describe the configuration rather than the traffic. */}
              {data?.plates_active !== false && (
                <PlateCoverage read={platesRead} vehicles={vehiclesSeen}
                               distinct={distinctPlates} />
              )}
            </div>
          </section>
        </div>

        {/* Two ranked bar lists, side by side. They are the same visual form
            answering two halves of one question — WHERE the activity is and
            WHAT it is — so they belong in one row rather than stacked full
            width, where each wasted two thirds of the page on four short bars.
            Lifting the vehicle mix out of Breakdown is also what balances the
            row above: that card was carrying three unrelated things and ran
            165px taller than the chart beside it. */}
        <div className="aid-duo">
          <section className="panel">
            <div className="panel-head"><div className="panel-head-l">
              <div className="panel-title">Top cameras</div>
            </div></div>
            <div className="panel-body">
              {(s?.top_cameras ?? []).map((c) => (
                <div key={c.camera} className="aid-cam">
                  <div className="aid-cam-head"><b>{c.camera}</b><span>{c.count.toLocaleString()}</span></div>
                  <div className="aid-cam-track">
                    <div className="aid-cam-fill" style={{ width: `${(c.count / maxCam) * 100}%` }} />
                  </div>
                </div>
              ))}
              {!s?.top_cameras?.length && <div className="aid-none">No detections in range.</div>}
            </div>
          </section>

          {/* The vehicle-class mix. The API has always returned by_type and
              nothing rendered it, so "1,296 vehicles" gave no clue whether a
              gate sees trucks or motorcycles — which is the question a gate
              camera exists to answer. Person is excluded: it has its own tile
              and would dwarf the rest. */}
          <section className="panel">
            <div className="panel-head"><div className="panel-head-l">
              <div className="panel-title">Vehicle mix</div>
            </div></div>
            <div className="panel-body">
              {vehicleTypes.map(([type, n]) => (
                <div key={type} className="aid-cam">
                  <div className="aid-cam-head">
                    <b>{type}</b><span>{n.toLocaleString()}</span>
                  </div>
                  <div className="aid-cam-track">
                    <div className="aid-cam-fill"
                         style={{ width: `${(n / maxType) * 100}%` }} />
                  </div>
                </div>
              ))}
              {!vehicleTypes.length && (
                <div className="aid-none">No vehicle classes in range.</div>
              )}
            </div>
          </section>
        </div>

        <PlatesPanel active={data?.plates_active} recent={recentPlates}
                     top={topPlates} onOpen={openPlate} />
      </div>
      )}
    </div>
  );
}
