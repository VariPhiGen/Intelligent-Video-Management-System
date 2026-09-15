/**
 * SearchFilters.tsx — the query box, the example chips, and the per-domain
 * filter row.
 *
 * The people and vehicle filters are genuinely different sets (a time window vs
 * a plate), which is why this takes the whole search state rather than a
 * generic field list.
 */
import { DateTimeField } from '@/components/DateTimeField';
import type { SearchCamera, SortBy, SortDir } from '@/lib/smartsearch';

import { EXAMPLES, Field, type Mode } from './searchUi';
import { orderHint, orderOptions } from './sortOptions';

const VEHICLE_TYPES = ['car', 'truck', 'bus', 'motorcycle', 'van', 'other'];
const COLOURS = ['white', 'black', 'grey', 'silver', 'red', 'blue', 'green', 'yellow'];

export function SearchFilters({
  mode, query, setQuery, run, busy,
  camSlug, setCamSlug, camOptions, timeFrom, setTimeFrom, timeTo, setTimeTo,
  plate, setPlate, vehType, setVehType, colour, setColour,
  topK, setTopK, threshold, setThreshold,
  sortBy, setSortBy, sortDir, setSortDir,
}: {
  mode: Mode;
  query: string;
  setQuery: (v: string) => void;
  run: () => void;
  busy: boolean;
  camSlug: string;
  setCamSlug: (v: string) => void;
  camOptions: SearchCamera[];
  timeFrom: string;
  setTimeFrom: (v: string) => void;
  timeTo: string;
  setTimeTo: (v: string) => void;
  plate: string;
  setPlate: (v: string) => void;
  vehType: string;
  setVehType: (v: string) => void;
  colour: string;
  setColour: (v: string) => void;
  topK: number;
  setTopK: (v: number) => void;
  threshold: number;
  setThreshold: (v: number) => void;
  sortBy: SortBy;
  setSortBy: (v: SortBy) => void;
  sortDir: SortDir;
  setSortDir: (v: SortDir) => void;
}) {
  return (
    <div className="panel ss-query">
      <div className="panel-body">
        <div className="ss-searchrow">
          <input
            className="ss-input"
            placeholder={mode === 'people' ? 'e.g. person in a red jacket' : 'e.g. white van'}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') run(); }}
            aria-label="Search query"
          />
          <button className="btn-primary" onClick={run} disabled={busy || !query.trim()}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>

        <div className="chips ss-examples">
          {EXAMPLES[mode].map(ex => (
            <button key={ex} className="chip" onClick={() => setQuery(ex)}>{ex}</button>
          ))}
        </div>

        <div className="ss-filters">
          {/* Camera and the time window belong to BOTH domains. They were
              drawn for people only, so a vehicle search silently covered every
              camera and every day the index holds — see routers/search.py. */}
          <Field label="Camera">
            <select
              value={camSlug}
              onChange={e => setCamSlug(e.target.value)}
              disabled={camOptions.length === 0}
            >
              <option value="">{camOptions.length ? 'Any recorded camera' : 'No recorded cameras'}</option>
              {camOptions.map(c => (
                <option key={c.slug} value={c.slug}>{c.name || c.slug}</option>
              ))}
            </select>
          </Field>
          {/* Split date + time: datetime-local's picker sets the day only in
              Chromium, so a search window silently started at 00:00. */}
          <Field label="From">
            <DateTimeField label="Search window start" value={timeFrom} onChange={setTimeFrom} />
          </Field>
          <Field label="To">
            <DateTimeField label="Search window end" value={timeTo} onChange={setTimeTo} />
          </Field>
          {mode === 'vehicles' && (
            <>
              <Field label="Type">
                <select value={vehType} onChange={e => setVehType(e.target.value)}>
                  <option value="">Any</option>
                  {VEHICLE_TYPES.map(v => <option key={v} value={v}>{v}</option>)}
                </select>
              </Field>
              <Field label="Colour">
                <select value={colour} onChange={e => setColour(e.target.value)}>
                  <option value="">Any</option>
                  {COLOURS.map(v => <option key={v} value={v}>{v}</option>)}
                </select>
              </Field>
            </>
          )}
          <Field label="Results">
            <select value={topK} onChange={e => setTopK(Number(e.target.value))}>
              {[12, 24, 48, 96].map(n => <option key={n} value={n}>{n}</option>)}
            </select>
          </Field>
          <Field label="Min. score">
            <select value={threshold} onChange={e => setThreshold(Number(e.target.value))}>
              <option value={0}>Any — show nearest, however weak</option>
              <option value={0.10}>0.10 — broad</option>
              <option value={0.15}>0.15 — balanced</option>
              <option value={0.17}>0.17 — strict</option>
            </select>
          </Field>
          {/* ORDERING, NOT FILTERING: this arranges the results a search
              already found, and nothing weaker gets in because of it. ONE
              field at a time — the mode picks which, and the other is not
              consulted. See index/queries.py Order. */}
          <Field label="Sort by">
            <select value={sortBy} onChange={e => setSortBy(e.target.value as SortBy)}
                    title="Which field arranges the page. The other one is not used at all.">
              <option value="time">Time</option>
              <option value="confidence">Confidence</option>
            </select>
          </Field>
          <Field label="Order">
            <select value={sortDir} onChange={e => setSortDir(e.target.value as SortDir)}
                    title={orderHint(sortBy)}>
              {orderOptions(sortBy).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </Field>
        </div>
      </div>
    </div>
  );
}
