/**
 * ANPR lookup — a plate is an exact identifier, so this is not a ranked search.
 *
 * It lives apart from People/Vehicles on purpose. Those ask "what looked like
 * this?" and answer with a similarity-ranked grid. A plate asks "where has this
 * vehicle been?", and the answer is a set of sightings in time order. Keeping it
 * as a filter on the Vehicles tab forced the operator to invent a description
 * alongside the plate, and then ranked exact matches by how well they resembled
 * that invented description.
 *
 * It also needs no text encoder, so it keeps working on an index whose model
 * failed to load.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { searchPlates, type PlateGroup, type PlateSearchResult, type SearchCamera } from '@/lib/smartsearch';
import { DateTimeField } from '@/components/DateTimeField';
import { EventClipModal } from './EventClipModal';
import { Field } from './searchUi';

function when(ms: number): string {
  return new Date(ms).toLocaleString();
}

const dayOf = (ms: number) => new Date(ms).toLocaleDateString();
const timeOf = (ms: number) => new Date(ms).toLocaleTimeString();

/** "16:53:26 → 17:19:04" within a day, the full date on both ends across days.
 *  A truck through a gate is usually one day, and printing the same date twice
 *  buries the part that differs. */
function span(fromMs: number, toMs: number): string {
  return dayOf(fromMs) === dayOf(toMs)
    ? `${dayOf(fromMs)}, ${timeOf(fromMs)} → ${timeOf(toMs)}`
    : `${when(fromMs)} → ${when(toMs)}`;
}

/** `YYYY-MM-DDTHH:mm` in LOCAL time — the format DateTimeField emits and `run`
 *  parses. Building it from the ISO string would shift the window by the
 *  timezone offset, which on a plate lookup silently loses the last few hours. */
function localStamp(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
       + `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** The common case is "recently", and four native date boxes is a lot of
 *  ceremony for it. `hours: null` = no bound at all. */
const WINDOWS: { label: string; hours: number | null }[] = [
  { label: 'Any time', hours: null },
  { label: '24 hours', hours: 24 },
  { label: '7 days', hours: 24 * 7 },
  { label: '30 days', hours: 24 * 30 },
];

export function AnprPanel({ initialPlate = '' }: { initialPlate?: string }) {
  const [plate, setPlate] = useState(initialPlate);
  /** Event-clip popup: null when closed, else the sighting to play (~20s).
   *
   *  The same treatment People and Vehicles get. This panel used to navigate
   *  away to the Playback timeline from a button labelled "Open Playback" —
   *  which is what ResultCard's button SAYS, but not what it does: the
   *  navigate-away action there is commented out and the visible button opens
   *  this popup. Two identically labelled buttons doing different things is the
   *  worse half of that; a plate lookup is a scan down a list of sightings, and
   *  losing the list to a full page change on the first one you check is the
   *  wrong trade. */
  const [eventClip, setEventClip] = useState<{ cam: SearchCamera; whenMs: number } | null>(null);
  const [timeFrom, setTimeFrom] = useState('');
  const [timeTo, setTimeTo] = useState('');
  const [windowKey, setWindowKey] = useState('Any time');
  const [custom, setCustom] = useState(false);
  const [res, setRes] = useState<PlateSearchResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = useCallback(async () => {
    const q = plate.trim();
    if (!q) return;
    setBusy(true); setErr(null);
    try {
      setRes(await searchPlates({
        plate: q,
        time_from: timeFrom ? new Date(timeFrom).toISOString() : null,
        time_to: timeTo ? new Date(timeTo).toISOString() : null,
      }));
    } catch (e: any) {
      setErr(e.message || 'Plate lookup failed');
      setRes(null);
    }
    setBusy(false);
  }, [plate, timeFrom, timeTo]);

  // Seeded from ?plate= — the AI-detections dashboard lists the plates it has
  // read, and clicking one has to arrive with the answer, not with a filled-in
  // box and a button still to press. Once only: `run` changes identity on every
  // keystroke, so depending on it here would re-search as the operator types.
  const ranSeed = useRef(false);
  useEffect(() => {
    if (!ranSeed.current && initialPlate.trim()) {
      ranSeed.current = true;
      run();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPlate]);

  /** Presets write the same two fields Custom edits, so there is one source of
   *  truth for the window and switching between them never strands a bound. */
  function applyWindow(w: { label: string; hours: number | null }) {
    setWindowKey(w.label);
    setCustom(false);
    setTimeTo('');
    setTimeFrom(w.hours == null
      ? ''
      : localStamp(new Date(Date.now() - w.hours * 3600_000)));
  }

  const searched = res !== null;
  const groups: PlateGroup[] = res?.plates ?? [];

  return (
    <>
      {eventClip && (
        <EventClipModal
          cam={eventClip.cam}
          whenMs={eventClip.whenMs}
          onClose={() => setEventClip(null)}
        />
      )}
      <div className="panel">
        <div className="panel-body">
          <div className="anpr-form">
            <div>
              {/* The plate and the action are one object, so they share one
                  frame. The wrapper carries the focus ring, which is why the
                  input inside has no chrome of its own. */}
              <div className="anpr-plate">
                <input
                  className="anpr-plate-input"
                  aria-label="Plate"
                  placeholder="MH12DE1433"
                  autoComplete="off"
                  spellCheck={false}
                  value={plate}
                  onChange={e => setPlate(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') run(); }}
                />
                <button className="btn-primary" disabled={busy || !plate.trim()} onClick={run}>
                  {busy ? 'Searching…' : 'Search'}
                </button>
              </div>
              {/* The hint does one job: teach that partial works. The
                  placeholder used to carry "e.g." and "or just DE14" as well,
                  which made it an instruction rendered in placeholder grey and
                  shouted in uppercase by the field's own text-transform. */}
              <div className="anpr-hint">
                Partial plates match — <code>DE14</code> finds <code>MH12DE1433</code>{'.'}
              </div>
            </div>

            <div className="anpr-when">
              <span className="anpr-when-label">When</span>
              <div className="chips">
                {WINDOWS.map(w => (
                  <button key={w.label} type="button"
                          className={`chip${!custom && windowKey === w.label ? ' active' : ''}`}
                          onClick={() => applyWindow(w)}>
                    {w.label}
                  </button>
                ))}
                <button type="button" className={`chip${custom ? ' active' : ''}`}
                        aria-expanded={custom}
                        onClick={() => { setCustom(c => !c); setWindowKey(''); }}>
                  Custom…
                </button>
              </div>
            </div>

            {custom && (
              <div className="anpr-custom">
                <Field label="From">
                  <DateTimeField label="Sighting window start" value={timeFrom} onChange={setTimeFrom} />
                </Field>
                <Field label="To">
                  <DateTimeField label="Sighting window end" value={timeTo} onChange={setTimeTo} />
                </Field>
              </div>
            )}
          </div>
        </div>
      </div>

      {err && <div className="panel row-err ss-error"><div className="panel-body">{err}</div></div>}
      {busy && <div className="ss-loading"><span className="spinner" /> Searching plates…</div>}

      {/* The index cannot read plates at all on this deployment. Saying "no
          sightings" here would be a statement about the vehicle, when the truth
          is that nothing has ever been read.

          Only when there is nothing to show: rows CAN exist while the reader is
          currently off — plates read before it was disabled, or written by
          another writer — and telling an operator that plate reading is not
          enabled while listing sightings underneath it is a contradiction. */}
      {!busy && searched && res?.plates_active === false && groups.length === 0 && (
        <div className="emptystate">
          <div className="empty">
            <b>Plate reading is not enabled on this deployment.</b>
            <div className="hint">
              No plates have been read, so no lookup can match — this is not a
              statement about which vehicles passed. Plate reading needs
              plate-detection weights configured on the index service
              (<code>SEARCH_PLATE_WEIGHTS</code>). Recording, playback and the
              People and Vehicles searches are unaffected.
            </div>
          </div>
        </div>
      )}

      {!busy && searched && res?.plates_active !== false && groups.length === 0 && (
        <div className="emptystate">
          <div className="empty">
            <b>No sightings.</b>
            <div className="hint">
              No plate matching <b>{plate.trim().toUpperCase()}</b> was read on the
              cameras this VMS records, within the window given. Try fewer
              characters — partial plates match — or widen the time range.
            </div>
          </div>
        </div>
      )}

      {!busy && groups.length > 0 && (
        <>
          <div className="ss-count">
            {res?.sightings} sighting{res?.sightings === 1 ? '' : 's'} of{' '}
            {groups.length} plate{groups.length === 1 ? '' : 's'}, most recent first
          </div>
          {groups.map(g => (
            <div className="panel" key={g.plate}>
              <div className="panel-head">
                <div className="panel-head-l">
                  <div>
                    <div className="anpr-tag">{g.plate}</div>
                    <div className="panel-sub">
                      {g.count} sighting{g.count === 1 ? '' : 's'} ·{' '}
                      {g.cameras.length} camera{g.cameras.length === 1 ? '' : 's'} ·{' '}
                      {span(g.first_seen_ms, g.last_seen_ms)}
                    </div>
                  </div>
                </div>
              </div>
              <div className="panel-body">
                <table className="table">
                  <thead>
                    <tr><th>When</th><th>Camera</th><th>Type</th><th /></tr>
                  </thead>
                  <tbody>
                    {g.sightings.map((s, i) => {
                      // The date is printed once per day, not once per row.
                      // Every sighting in a group is usually the same day, so
                      // repeating it pushed the times — the thing being scanned
                      // — behind eleven identical characters.
                      const prev = i > 0 ? g.sightings[i - 1] : null;
                      const newDay = !prev || dayOf(prev.when_ms) !== dayOf(s.when_ms);
                      return (
                      <tr key={s.id}>
                        <td>
                          {newDay && (
                            <div style={{ fontSize: 11, color: 'var(--dim)' }}>{dayOf(s.when_ms)}</div>
                          )}
                          <span className="mono">{timeOf(s.when_ms)}</span>
                        </td>
                        <td>{s.camera.name || s.camera.slug}</td>
                        <td>{s.vehicle_type || '—'}</td>
                        <td style={{ textAlign: 'right' }}>
                          {/* Jumping to the full Playback timeline instead —
                              same action ResultCard keeps commented out, kept
                              here for the same reason:
                          <button className="btn-sm" onClick={() => nav(
                            `/playback?cam=${encodeURIComponent(s.camera.slug)}` +
                            `&t=${Math.floor(s.when_ms / 1000)}`
                          )}>Open in Playback</button>
                          */}
                          <button
                            className="btn-secondary btn-sm"
                            title="Play ~20s of this sighting in a popup"
                            onClick={() => setEventClip({ cam: s.camera, whenMs: s.when_ms })}
                          >
                            ⏱ Open Playback
                          </button>
                        </td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </>
      )}

      {!busy && !searched && (
        <div className="anpr-prompt">
          <div>
            <b>Enter a plate to see every sighting.</b>
            <div className="hint">
              Results group by plate in time order, and each sighting plays as a
              short clip without leaving the list.
            </div>
          </div>
        </div>
      )}
    </>
  );
}
