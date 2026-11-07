/**
 * timelineMarks.ts — AI markers drawn on the playback timeline.
 *
 * Two sources, up to four rows:
 *   * person / vehicle / motion — PRESENCE (`GET /api/playback/timeline`,
 *     services/timeline.py): when a tracked object was in view, or when the
 *     camera saw motion, merged by the API at this timeline's pixel
 *     resolution. Only for kinds the camera produces: person / vehicle with
 *     Smart Search indexing on, motion with motion detection on.
 *   * events — the stored AI activity events (`GET /api/analytics/events`,
 *     services/ai_events.py): what a camera's CONFIGURED activities decided.
 *
 * Both become TimelineMarks, so one hit test, one legend and one next/previous
 * walk cover all of them; `row` says where a mark is drawn.
 *
 * NO MARKER WITHOUT FOOTAGE (`clipToFootage`). Analytics and motion watch the
 * live relay, not the recording, so they keep reporting through a recording
 * gap, and a marker can arrive before the NVR has indexed its minute. A mark
 * the operator cannot play is a promise the page cannot keep: every mark is cut
 * to the NVR's coverage before it is drawn or walked to.
 *
 * The browser fetches the visible window plus half a view either side, so a
 * short pan is already painted, and never more than MAX_MARKS events. The API
 * pages newest first, so a capped window keeps its newest events and reports
 * `truncated` rather than pretending it is complete.
 */
import { useEffect, useRef, useState } from 'react';
import { ApiError } from '@/lib/api';
import { apiFetch } from '@/lib/api';
import { fetchEvents, type AiEvent } from '@/lib/aiEvents';
import type { Camera } from '@/lib/types';

export type PresenceKind = 'person' | 'vehicle' | 'motion';
export type MarkRow = PresenceKind | 'event';

export interface TimelineMark {
  id: string;
  /** Which strip row it is drawn in. */
  row: MarkRow;
  /** Presence only: how many track segments this bar merged. */
  count?: number;
  /** Epoch seconds the event started — where the mark is drawn. */
  start: number;
  /** Epoch seconds it ended; null for a point event (or one still open). */
  end: number | null;
  /** Epoch seconds to seek to: the API's playback window, pre-roll included. */
  seekAt: number;
  activity: string;
  label: string;
  /** #RRGGBB from the activity catalog. */
  color: string;
  zone: string | null;
  objectClass: string | null;
  confidence: number | null;
}

/** Indexed presence kind → the AI Config search domain that switches it on. */
const DOMAIN_OF: Record<'person' | 'vehicle', string> = { person: 'person', vehicle: 'vehicles' };

/** Presence bars seek this far before the object appeared. */
export const PRESENCE_PRE_ROLL_S = 3;

export const PRESENCE_META: Record<PresenceKind, { key: string; label: string; color: string }> = {
  person: { key: 'presence:person', label: 'Person', color: 'var(--tl-person)' },
  vehicle: { key: 'presence:vehicle', label: 'Vehicle', color: 'var(--tl-vehicle)' },
  motion: { key: 'presence:motion', label: 'Motion', color: 'var(--tl-motion)' },
};

/** Presence kinds this camera produces, in row order: person / vehicle when
 *  indexing is on with the domain ticked (`search_domains` means nothing while
 *  `search_indexing` is off, types.ts), motion when motion detection is on. */
export function indexedKinds(
  cam: Pick<Camera, 'search_indexing' | 'search_domains'> & { motion_detection?: boolean } | undefined,
): PresenceKind[] {
  if (!cam) return [];
  const out: PresenceKind[] = [];
  if (cam.search_indexing === true) {
    const domains = cam.search_domains ?? [];
    for (const k of ['person', 'vehicle'] as const) if (domains.includes(DOMAIN_OF[k])) out.push(k);
  }
  if (cam.motion_detection === true) out.push('motion');
  return out;
}

/**
 * Whether a camera's timeline gets a marker strip at all: only when it is
 * configured to produce something to mark — at least one AI activity, Smart
 * Search indexing switched on for people or vehicles, or motion detection. A
 * camera running none of these shows the plain footage bar rather than an
 * empty strip that looks broken.
 */
export function cameraHasAi(
  cam: Pick<Camera, 'analytics_config' | 'search_indexing' | 'search_domains'>
    & { motion_detection?: boolean } | undefined,
): boolean {
  if (!cam) return false;
  if ((cam.analytics_config?.activities?.length ?? 0) > 0) return true;
  return indexedKinds(cam).length > 0;
}

/** Hard cap on events held for one window: 5 pages of the API's maximum. */
export const MAX_MARKS = 1000;
const PAGE = 200;
/** Fallback when an event carries no usable colour. */
export const MARK_FALLBACK_COLOR = '#A855F7';

const HEX = /^#[0-9a-f]{6}$/i;

export function toMark(ev: AiEvent): TimelineMark | null {
  const start = Date.parse(ev.started_at) / 1000;
  if (!Number.isFinite(start)) return null;
  const endRaw = ev.ended_at ? Date.parse(ev.ended_at) / 1000 : NaN;
  const end = Number.isFinite(endRaw) && endRaw > start ? endRaw : null;
  const seek = ev.playback && Number.isFinite(ev.playback.start) ? ev.playback.start : start;
  return {
    id: ev.id,
    row: 'event',
    start,
    end,
    seekAt: Math.min(seek, start),
    activity: ev.activity?.key ?? '',
    label: ev.activity?.label || ev.activity?.key || 'Event',
    color: HEX.test(ev.activity?.color ?? '') ? ev.activity.color : MARK_FALLBACK_COLOR,
    zone: ev.zone,
    objectClass: ev.object_class,
    confidence: ev.confidence,
  };
}

/** One merged presence bar as the API sends it: epoch seconds + segments merged. */
export type PresenceBar = [number, number, number];

export function presenceMark(kind: PresenceKind, [start, end, n]: PresenceBar): TimelineMark {
  const meta = PRESENCE_META[kind];
  return {
    id: `${meta.key}:${start}`,
    row: kind,
    count: n,
    start,
    end: end > start ? end : null,
    seekAt: start - PRESENCE_PRE_ROLL_S,
    activity: meta.key,
    label: meta.label,
    color: meta.color,
    zone: null,
    objectClass: null,
    confidence: null,
  };
}

interface PresenceResponse {
  kinds: Partial<Record<PresenceKind, PresenceBar[]>>;
  truncated: boolean;
}

/** Presence for the window, merged at `px` resolution by the API. */
async function fetchPresence(camera: string, fromMs: number, toMs: number,
                             kinds: PresenceKind[], px: number):
    Promise<{ marks: TimelineMark[]; truncated: boolean }> {
  const p = new URLSearchParams({
    camera, from: new Date(fromMs).toISOString(), to: new Date(toMs).toISOString(),
    kinds: kinds.join(','), px: String(Math.min(8000, Math.max(100, Math.round(px)))),
  });
  const r = await apiFetch<PresenceResponse>(`/playback/timeline?${p}`);
  const marks: TimelineMark[] = [];
  for (const k of kinds) for (const bar of r.kinds?.[k] ?? []) marks.push(presenceMark(k, bar));
  return { marks, truncated: !!r.truncated };
}

/**
 * Cut marks to the footage the NVR actually holds. `footage` is covered spans
 * in epoch MILLISECONDS (coverage.ts `covSpans`: window ∩ recorded extent minus
 * gaps). A mark with no footage under it is dropped; a bar crossing a gap is
 * split into its playable pieces; a point event survives only on footage.
 * The seek point never lands before the footage it belongs to — a pre-roll
 * that would reach into a gap starts where that footage starts instead.
 */
export function clipToFootage(marks: TimelineMark[], footage: [number, number][]): TimelineMark[] {
  const spans = footage.map(([a, b]) => [a / 1000, b / 1000] as const).filter(([a, b]) => b > a);
  const out: TimelineMark[] = [];
  for (const m of marks) {
    if (m.end == null) {
      const span = spans.find(([a, b]) => m.start >= a && m.start <= b);
      if (span) out.push({ ...m, seekAt: Math.max(m.seekAt, span[0]) });
      continue;
    }
    const pieces = spans
      .map(([a, b]) => [Math.max(a, m.start), Math.min(b, m.end!), a] as const)
      .filter(([s, e]) => e > s);
    pieces.forEach(([s, e, spanStart], i) => {
      out.push({
        ...m,
        id: pieces.length > 1 ? `${m.id}#${i}` : m.id,
        start: s,
        end: e,
        // The first piece keeps its pre-roll, cut at its footage's start; a
        // later piece begins at a gap's end, and plays from there.
        seekAt: s === m.start ? Math.max(m.seekAt, spanStart) : s,
      });
    });
  }
  return out;
}

/** Oldest first — the order the next/previous buttons walk in. */
export function sortMarks(marks: TimelineMark[]): TimelineMark[] {
  return [...marks].sort((a, b) => a.start - b.start || a.id.localeCompare(b.id));
}

/**
 * The mark to jump to from `playhead` (epoch seconds), compared on the SEEK
 * target: a jump lands `seekAt` (pre-roll) before the mark, and comparing on
 * `start` would make "next" find the mark just jumped to again.
 */
export function adjacentMark(sorted: TimelineMark[], playhead: number | null,
                             dir: 1 | -1): TimelineMark | null {
  if (!sorted.length) return null;
  if (playhead == null) return dir === 1 ? sorted[0] : sorted[sorted.length - 1];
  if (dir === 1) return sorted.find(m => m.seekAt > playhead + 1) ?? null;
  // Backwards skips the mark whose pre-roll is playing now, as a media player's
  // "previous" does once a track is a few seconds in.
  for (let i = sorted.length - 1; i >= 0; i--) if (sorted[i].seekAt < playhead - 3) return sorted[i];
  return null;
}

/**
 * The mark under pixel `x`, or null. Marks are drawn at least `minPx` wide, so
 * a point event is hit anywhere on its glyph, and a short interval is hit on
 * its minimum width; the closest start wins where they overlap.
 */
export function hitMark(marks: TimelineMark[], x: number, toPx: (epochSec: number) => number,
                        slopPx = 5): TimelineMark | null {
  let best: TimelineMark | null = null;
  let bestD = Infinity;
  for (const m of marks) {
    const x1 = toPx(m.start);
    const x2 = m.end != null ? Math.max(toPx(m.end), x1) : x1;
    const inside = x >= x1 - slopPx && x <= x2 + slopPx;
    if (!inside) continue;
    const d = Math.abs(x - x1);
    if (d < bestD) { best = m; bestD = d; }
  }
  return best;
}

async function fetchWindow(camera: string, fromMs: number, toMs: number, signal: AbortSignal):
    Promise<{ marks: TimelineMark[]; truncated: boolean }> {
  const out: TimelineMark[] = [];
  let before: string | null = null;
  const from = new Date(fromMs).toISOString();
  const to = new Date(toMs).toISOString();
  for (;;) {
    if (signal.aborted) throw new DOMException('aborted', 'AbortError');
    const page = await fetchEvents({ camera, from, to, before, limit: PAGE });
    for (const ev of page.events) {
      const m = toMark(ev);
      if (m) out.push(m);
    }
    before = page.next_before;
    if (!before) return { marks: out, truncated: false };
    if (out.length >= MAX_MARKS) return { marks: out, truncated: true };
  }
}

/** Events and presence for one camera and window, as one sorted mark list. */
export async function fetchMarks(camera: string, fromMs: number, toMs: number,
                                 kinds: PresenceKind[], px: number, signal: AbortSignal):
    Promise<{ marks: TimelineMark[]; truncated: boolean }> {
  const [ev, pr] = await Promise.all([
    fetchWindow(camera, fromMs, toMs, signal),
    kinds.length ? fetchPresence(camera, fromMs, toMs, kinds, px)
                 : Promise.resolve({ marks: [] as TimelineMark[], truncated: false }),
  ]);
  return { marks: sortMarks([...pr.marks, ...ev.marks]), truncated: ev.truncated || pr.truncated };
}

/** What `GET /playback/timeline/next` answers: the nearest marker anywhere in
 *  the kept footage, when none is loaded in the direction asked. */
export interface AdjacentMarker {
  row: MarkRow;
  activity: string | null;
  start: number;
  end: number | null;
  /** Epoch seconds to seek to — pre-roll included, as for a loaded mark. */
  seek_at: number;
}

export async function fetchAdjacent(camera: string, atSec: number, dir: 1 | -1,
                                    kinds: PresenceKind[], events: boolean,
                                    hiddenActivities: string[]): Promise<AdjacentMarker | null> {
  const p = new URLSearchParams({
    camera, at: new Date(atSec * 1000).toISOString(), dir: dir === 1 ? 'next' : 'prev',
    kinds: kinds.join(','), events: String(events),
  });
  if (hiddenActivities.length) p.set('hide', hiddenActivities.join(','));
  const r = await apiFetch<{ marker: AdjacentMarker | null }>(`/playback/timeline/next?${p}`);
  return r.marker;
}

/** A server answer as a mark, so it goes through the same footage cut and seek. */
export function toMarkFromAdjacent(m: AdjacentMarker): TimelineMark {
  return {
    id: `adjacent:${m.row}:${m.start}`, row: m.row, start: m.start,
    end: m.end != null && m.end > m.start ? m.end : null, seekAt: m.seek_at,
    activity: m.activity ?? '', label: '', color: '', zone: null, objectClass: null,
    confidence: null,
  };
}

export type MarksStatus = 'off' | 'loading' | 'ready' | 'unavailable';

/**
 * Events and presence for `camera` around the view, refetched when the view
 * moves by more than an eighth of its span and every minute while the view
 * reaches the live edge. A 403/404 (no `ai_analytics`, camera not registered)
 * switches the feature off for this camera instead of retrying forever.
 * `kinds` are the presence kinds to fetch (none = events only); `widthPx` is
 * the timeline's width, which sets how finely the API merges presence.
 */
export function useTimelineMarks(camera: string, view: { start: number; end: number },
                                 enabled: boolean, kinds: PresenceKind[] = [], widthPx = 1200) {
  const [marks, setMarks] = useState<TimelineMark[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [status, setStatus] = useState<MarksStatus>(enabled ? 'loading' : 'off');
  const [tick, setTick] = useState(0);
  const keyRef = useRef('');
  const deniedRef = useRef<string | null>(null);

  // Camera switch: drop the previous camera's marks at once — they are not
  // this footage's events, even for the moment before the new fetch lands.
  useEffect(() => {
    setMarks([]);
    setTruncated(false);
    keyRef.current = '';
    deniedRef.current = null;
  }, [camera]);

  const nearLive = view.end >= Date.now() - 5 * 60_000;
  useEffect(() => {
    if (!enabled || !nearLive) return;
    const t = setInterval(() => { keyRef.current = ''; setTick(n => n + 1); }, 60_000);
    return () => clearInterval(t);
  }, [enabled, nearLive]);

  useEffect(() => {
    if (!enabled) { setStatus('off'); keyRef.current = ''; return; }
    if (deniedRef.current === camera) { setStatus('unavailable'); return; }
    const span = Math.max(60_000, view.end - view.start);
    const step = span / 8;
    const from = Math.floor((view.start - span / 2) / step) * step;
    const to = Math.ceil((view.end + span / 2) / step) * step;
    const kindsKey = kinds.join(',');
    const key = `${camera}|${from}|${to}|${kindsKey}`;
    if (key === keyRef.current) return;
    const ctl = new AbortController();
    const timer = setTimeout(async () => {
      setStatus('loading');
      try {
        // The fetched window is wider than the view; scale the pixels with it.
        const px = widthPx * (to - from) / Math.max(1, view.end - view.start);
        const r = await fetchMarks(camera, from, to, kinds, px, ctl.signal);
        if (ctl.signal.aborted) return;
        keyRef.current = key;
        setMarks(r.marks);
        setTruncated(r.truncated);
        setStatus('ready');
      } catch (e) {
        if (ctl.signal.aborted) return;
        if (e instanceof ApiError && (e.status === 403 || e.status === 404)) {
          deniedRef.current = camera;
          setMarks([]);
          setStatus('unavailable');
          return;
        }
        // Transient (API restarting): keep what is drawn; the next pan retries.
        setStatus('ready');
      }
    }, 300);
    return () => { clearTimeout(timer); ctl.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera, view.start, view.end, enabled, tick, kinds.join(','), widthPx]);

  return { marks: enabled ? marks : [], truncated, status };
}

/**
 * The multi-camera player's version: marks for each lane's camera, fetched
 * together for the view plus half a view either side. `lanes` pairs each
 * selected camera with the presence kinds it produces, or null for a camera
 * with no AI configured — which gets no marks and costs no request. A camera
 * the API refuses (403/404) simply has none; the lanes never block on it.
 */
export function useLaneMarks(lanes: { camera: string; kinds: PresenceKind[] | null }[],
                             view: { start: number; end: number }, enabled: boolean,
                             widthPx = 1200): Record<string, TimelineMark[]> {
  const [byCam, setByCam] = useState<Record<string, TimelineMark[]>>({});
  const lanesKey = lanes.map(l => `${l.camera}:${l.kinds?.join('+') ?? '-'}`).join(',');
  const span = Math.max(60_000, view.end - view.start);
  const step = span / 8;
  const from = Math.floor((view.start - span / 2) / step) * step;
  const to = Math.ceil((view.end + span / 2) / step) * step;

  useEffect(() => {
    if (!enabled) { setByCam({}); return; }
    const ctl = new AbortController();
    const timer = setTimeout(async () => {
      const px = widthPx * (to - from) / span;
      const got = await Promise.all(lanes.map(async l => {
        if (!l.kinds) return [l.camera, []] as const;
        try {
          return [l.camera, (await fetchMarks(l.camera, from, to, l.kinds, px, ctl.signal)).marks] as const;
        } catch {
          return [l.camera, []] as const;
        }
      }));
      if (!ctl.signal.aborted) setByCam(Object.fromEntries(got));
    }, 300);
    return () => { clearTimeout(timer); ctl.abort(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lanesKey, from, to, enabled, widthPx]);

  return enabled ? byCam : {};
}
