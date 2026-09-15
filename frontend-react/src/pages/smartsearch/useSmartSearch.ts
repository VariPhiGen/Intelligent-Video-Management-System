/**
 * useSmartSearch.ts — every piece of state a Smart Search query needs, and the
 * call that runs it.
 *
 * Pulled out of SmartSearchPage so the page is layout and this is behaviour.
 * Two things here are load-bearing and easy to undo by accident:
 *   • queries are PER TAB — switching people/vehicles must not carry the other
 *     tab's term into the box;
 *   • a camera can leave the searchable set while it is selected, and holding
 *     the stale slug turns the next search into a 404, so it is reset.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { ApiError } from '@/lib/api';
import {
  indexStats, searchPeople, searchVehicles, searchableCameras, summariseWithheld,
  type DomainStats, type PersonHit, type SearchCamera, type SortBy, type SortDir,
  type VehicleHit, type Withheld,
} from '@/lib/smartsearch';

import type { Mode } from './searchUi';

/** One search's outcome, whichever domain produced it. */
export interface Outcome<T> {
  results: T[];
  withheld: Withheld;
  matched: number;
  truncated: boolean;
}

export function useSmartSearch() {
  // Deep-link seed (e.g. #/smartsearch?q=<subject>&mode=people). Read once on
  // mount; local state after.
  const [sp] = useSearchParams();

  const [mode, setMode] = useState<Mode>(() => (sp.get('mode') === 'vehicles' ? 'vehicles' : 'people'));
  const [queries, setQueries] = useState<Record<Mode, string>>(() => {
    const q = sp.get('q') || '';
    const m = sp.get('mode') === 'vehicles' ? 'vehicles' : 'people';
    return { people: m === 'people' ? q : '', vehicles: m === 'vehicles' ? q : '' };
  });
  const query = queries[mode];
  const setQuery = (v: string) => setQueries(p => ({ ...p, [mode]: v }));

  const [topK, setTopK] = useState(60);
  const [page, setPage] = useState(0);
  // 0.05 never bound — it sat below the model's noise floor, so every query,
  // including a nonsense one, came back with a full page of `top_k` rows.
  const [threshold, setThreshold] = useState(0.15);

  // People filters. `camSlug` is a registry slug — the backend validates it
  // against the searchable set, so a stale one is a 404 rather than a silent
  // unfiltered search. `?cam=` seeds it from Live View's "search this camera".
  const [camSlug, setCamSlug] = useState(() => sp.get('cam') || '');
  const [timeFrom, setTimeFrom] = useState('');
  const [timeTo, setTimeTo] = useState('');
  const [camOptions, setCamOptions] = useState<SearchCamera[]>([]);
  const [recorderDown, setRecorderDown] = useState(false);

  // Ordering. Not a filter: it arranges the results a search found and never
  // decides which results there are. One field at a time — the default is the
  // page as it has always read, newest first.
  const [sortBy, setSortBy] = useState<SortBy>('time');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  // Vehicle filters
  const [plate, setPlate] = useState('');
  const [vehType, setVehType] = useState('');
  const [colour, setColour] = useState('');

  const [people, setPeople] = useState<Outcome<PersonHit> | null>(null);
  const [vehicles, setVehicles] = useState<Outcome<VehicleHit> | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [stats, setStats] = useState<Partial<Record<Mode, DomainStats>>>({});
  const [reachable, setReachable] = useState<boolean | null>(null);
  // Distinct from `reachable`: no index is deployed here at all, which is a
  // supported installation rather than an outage.
  const [configured, setConfigured] = useState<boolean | null>(null);

  // Index size doubles as a reachability probe, so the page can say "index
  // offline" up front instead of only failing once someone searches. The counts
  // are index-wide (it is shared with the analytics appliance); what a search
  // returns is still scoped, and `withheld` explains the difference.
  useEffect(() => {
    let dead = false;
    indexStats()
      .then(r => {
        if (dead) return;
        setStats(r.domains || {});
        setReachable(r.reachable);
        setConfigured(r.configured !== false);
      })
      .catch(() => { if (!dead) setReachable(false); });
    return () => { dead = true; };
  }, []);

  // Refreshed periodically so a camera added or fully groomed mid-session is
  // reflected in the filter without a reload.
  useEffect(() => {
    let dead = false;
    const load = () => searchableCameras()
      .then(r => {
        if (dead) return;
        setCamOptions(r.cameras || []);
        setRecorderDown(!r.recorder_available);
      })
      .catch(() => { /* filter list is a nicety; search works without it */ });
    load();
    const t = setInterval(load, 60000);
    return () => { dead = true; clearInterval(t); };
  }, []);

  // A camera can leave the searchable set while it is selected; holding a stale
  // slug would turn the next search into a 404.
  useEffect(() => {
    if (camSlug && camOptions.length && !camOptions.some(c => c.slug === camSlug)) {
      setCamSlug('');
    }
  }, [camSlug, camOptions]);

  const run = useCallback(async () => {
    const q = query.trim();
    if (!q) return;
    setBusy(true);
    setErr(null);
    try {
      if (mode === 'people') {
        setPeople(await searchPeople({
          query: q, top_k: topK, score_threshold: threshold,
          camera: camSlug || null,
          time_from: timeFrom ? new Date(timeFrom).toISOString() : null,
          time_to: timeTo ? new Date(timeTo).toISOString() : null,
          sort_by: sortBy, sort_dir: sortDir,
        }));
      } else {
        setVehicles(await searchVehicles({
          query: q, top_k: topK, score_threshold: threshold,
          plate: plate.trim().toUpperCase() || null,
          vehicle_type: vehType || null,
          color: colour || null,
          camera: camSlug || null,
          // The same bounds the person search sends. Left empty, the index
          // starts from its recent window instead.
          time_from: timeFrom ? new Date(timeFrom).toISOString() : null,
          time_to: timeTo ? new Date(timeTo).toISOString() : null,
          sort_by: sortBy, sort_dir: sortDir,
        }));
      }
      setReachable(true);
      setPage(0);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Search failed.');
      // 502 is the backend saying the index did not answer; anything else is a
      // bad query or a permission problem, and the index is fine.
      if (e instanceof ApiError && e.status === 502) setReachable(false);
      // 503 is the backend saying no index is deployed here.
      if (e instanceof ApiError && e.status === 503) { setReachable(false); setConfigured(false); }
    } finally {
      setBusy(false);
    }
  }, [mode, query, topK, threshold, camSlug, timeFrom, timeTo, plate, vehType, colour,
      sortBy, sortDir]);

  const switchMode = (m: Mode) => {
    if (m === mode) return;
    setMode(m);
    setErr(null);
    setPage(0);
  };

  const outcome: Outcome<PersonHit | VehicleHit> | null = mode === 'people' ? people : vehicles;
  const withheld = useMemo(() => summariseWithheld(outcome?.withheld || {}), [outcome]);
  /** Best similarity on the page. The index always returns its nearest
   *  neighbours, so a full page is not evidence that anything matched. */
  const topScore = outcome?.results.reduce((m, r) => Math.max(m, r.score), 0) ?? 0;

  return {
    mode, switchMode,
    query, setQuery,
    topK, setTopK, threshold, setThreshold,
    camSlug, setCamSlug, timeFrom, setTimeFrom, timeTo, setTimeTo,
    // Cameras the operator has opted out of indexing. The page needs these to
    // tell "found nothing" from "never looked here".
    unindexed: camOptions.filter(c => c.search_indexing === false),
    camOptions, recorderDown,
    plate, setPlate, vehType, setVehType, colour, setColour,
    sortBy, setSortBy, sortDir, setSortDir,
    people, vehicles, outcome, withheld, topScore,
    busy, err, stats, reachable, configured,
    page, setPage,
    run,
  };
}
