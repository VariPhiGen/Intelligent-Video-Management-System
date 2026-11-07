import { describe, expect, it } from 'vitest';
import type { AiEvent } from '@/lib/aiEvents';
import {
  adjacentMark, cameraHasAi, clipToFootage, hitMark, indexedKinds, MARK_FALLBACK_COLOR, presenceMark,
  PRESENCE_PRE_ROLL_S, sortMarks, toMark, type TimelineMark,
} from './timelineMarks';

const T0 = Date.parse('2026-09-23T10:00:00Z') / 1000;

function ev(over: Partial<AiEvent> = {}): AiEvent {
  return {
    id: 'e1',
    camera: { id: 'c', slug: 'gate', name: 'Gate' },
    activity: { key: 'intrusion', label: 'Intrusion', color: '#EF4444' },
    zone: 'Yard',
    started_at: new Date(T0 * 1000).toISOString(),
    ended_at: null,
    duration_s: null,
    confidence: 0.9,
    track_id: null,
    object_class: 'person',
    attributes: {},
    source: 'cpu',
    playback: { camera: 'gate', start: T0 - 10, end: T0 + 30 },
    ...over,
  };
}

function mark(id: string, start: number, seekAt = start - 10, end: number | null = null): TimelineMark {
  return { id, row: 'event', start, end, seekAt, activity: 'a', label: 'A', color: '#000000',
           zone: null, objectClass: null, confidence: null };
}

describe('toMark', () => {
  it('maps an event, seeking to the API pre-roll', () => {
    const m = toMark(ev())!;
    expect(m.start).toBe(T0);
    expect(m.end).toBeNull();
    expect(m.seekAt).toBe(T0 - 10);
    expect(m.label).toBe('Intrusion');
    expect(m.objectClass).toBe('person');
  });

  it('keeps an interval end only when it is after the start', () => {
    expect(toMark(ev({ ended_at: new Date((T0 + 42) * 1000).toISOString() }))!.end).toBe(T0 + 42);
    expect(toMark(ev({ ended_at: new Date((T0 - 5) * 1000).toISOString() }))!.end).toBeNull();
  });

  it('falls back on a colour the canvas could not use, and drops an unparseable time', () => {
    expect(toMark(ev({ activity: { key: 'x', label: 'X', color: 'red' } }))!.color).toBe(MARK_FALLBACK_COLOR);
    expect(toMark(ev({ started_at: 'nope' }))).toBeNull();
  });

  it('never seeks after the event itself', () => {
    expect(toMark(ev({ playback: { camera: 'gate', start: T0 + 99, end: T0 + 120 } }))!.seekAt).toBe(T0);
  });
});

describe('adjacentMark', () => {
  const marks = sortMarks([mark('c', T0 + 300), mark('a', T0 + 100), mark('b', T0 + 200)]);

  it('walks forward past the pre-roll it just jumped to', () => {
    // Jumped to b: the playhead sits on b's pre-roll, so "next" must be c.
    expect(adjacentMark(marks, T0 + 190, 1)?.id).toBe('c');
    expect(adjacentMark(marks, T0 + 50, 1)?.id).toBe('a');
    expect(adjacentMark(marks, T0 + 400, 1)).toBeNull();
  });

  it('walks back, skipping the mark whose pre-roll is playing', () => {
    expect(adjacentMark(marks, T0 + 191, -1)?.id).toBe('a');
    expect(adjacentMark(marks, T0 + 250, -1)?.id).toBe('b');
    expect(adjacentMark(marks, T0 + 50, -1)).toBeNull();
  });

  it('with no playhead, goes to the first or last', () => {
    expect(adjacentMark(marks, null, 1)?.id).toBe('a');
    expect(adjacentMark(marks, null, -1)?.id).toBe('c');
    expect(adjacentMark([], T0, 1)).toBeNull();
  });
});

describe('hitMark', () => {
  const toPx = (s: number) => s - T0;   // 1 px per second
  const marks = [mark('p', T0 + 100), mark('i', T0 + 200, T0 + 190, T0 + 260)];

  it('hits a point event within the slop', () => {
    expect(hitMark(marks, 103, toPx)?.id).toBe('p');
    expect(hitMark(marks, 110, toPx)).toBeNull();
  });

  it('hits anywhere along an interval', () => {
    expect(hitMark(marks, 240, toPx)?.id).toBe('i');
    expect(hitMark(marks, 270, toPx)).toBeNull();
  });

  it('prefers the nearest start where marks overlap', () => {
    const crowded = [mark('x', T0 + 100), mark('y', T0 + 104)];
    expect(hitMark(crowded, 103, toPx)?.id).toBe('y');
  });
});

describe('cameraHasAi', () => {
  const cam = (over: object = {}) => ({
    analytics_config: { regions: {}, activities: [] }, search_indexing: false, search_domains: [], ...over,
  });
  const act = { type: 'intrusion', regions: ['z1'], params: {} };

  it('is off for a camera running no AI, or one not in the registry', () => {
    expect(cameraHasAi(cam())).toBe(false);
    expect(cameraHasAi(undefined)).toBe(false);
  });

  it('is on with any configured activity', () => {
    expect(cameraHasAi(cam({ analytics_config: { regions: {}, activities: [act] } }))).toBe(true);
  });

  it('is on with person or vehicle indexing — but only while indexing is switched on', () => {
    expect(cameraHasAi(cam({ search_indexing: true, search_domains: ['person'] }))).toBe(true);
    expect(cameraHasAi(cam({ search_indexing: true, search_domains: ['vehicles'] }))).toBe(true);
    expect(cameraHasAi(cam({ search_indexing: false, search_domains: ['person', 'vehicles'] }))).toBe(false);
  });

  it('does not count plate- or face-only indexing', () => {
    expect(cameraHasAi(cam({ search_indexing: true, search_domains: ['plate', 'face'] }))).toBe(false);
  });
});

describe('indexedKinds', () => {
  const cam = (over: object = {}) => ({ search_indexing: true, search_domains: ['person', 'vehicles'], ...over });

  it('maps ticked domains to presence kinds, in row order', () => {
    expect(indexedKinds(cam())).toEqual(['person', 'vehicle']);
    expect(indexedKinds(cam({ search_domains: ['vehicles', 'plate'] }))).toEqual(['vehicle']);
  });

  it('is empty while indexing is off, or for plate/face only', () => {
    expect(indexedKinds(cam({ search_indexing: false }))).toEqual([]);
    expect(indexedKinds(cam({ search_domains: ['plate', 'face'] }))).toEqual([]);
    expect(indexedKinds(undefined)).toEqual([]);
  });
});

describe('presenceMark', () => {
  it('turns a merged bar into a mark in its own row, seeking a little before it', () => {
    const m = presenceMark('vehicle', [T0, T0 + 30, 2]);
    expect(m.row).toBe('vehicle');
    expect(m.label).toBe('Vehicle');
    expect([m.start, m.end, m.count]).toEqual([T0, T0 + 30, 2]);
    expect(m.seekAt).toBe(T0 - PRESENCE_PRE_ROLL_S);
    expect(m.activity).toBe('presence:vehicle');
  });

  it('gives bars of different kinds or starts different ids', () => {
    const a = presenceMark('person', [T0, T0 + 5, 1]);
    const b = presenceMark('vehicle', [T0, T0 + 5, 1]);
    const c = presenceMark('person', [T0 + 1, T0 + 5, 1]);
    expect(new Set([a.id, b.id, c.id]).size).toBe(3);
  });

  it('walks together with events in next/previous', () => {
    const marks = sortMarks([presenceMark('person', [T0 + 100, T0 + 110, 1]), mark('e', T0 + 200)]);
    expect(adjacentMark(marks, T0, 1)?.row).toBe('person');
    expect(adjacentMark(marks, T0 + 120, 1)?.row).toBe('event');
  });
});

describe('motion markers', () => {
  it('motion follows the camera\'s motion detection switch, after person/vehicle', () => {
    expect(indexedKinds({ search_indexing: true, search_domains: ['person'], motion_detection: true }))
      .toEqual(['person', 'motion']);
    expect(indexedKinds({ search_indexing: false, search_domains: [], motion_detection: true }))
      .toEqual(['motion']);
    expect(indexedKinds({ search_indexing: false, search_domains: [], motion_detection: false }))
      .toEqual([]);
  });

  it('a camera with only motion detection on gets the strip', () => {
    expect(cameraHasAi({ analytics_config: { regions: {}, activities: [] }, search_indexing: false,
                         search_domains: [], motion_detection: true })).toBe(true);
  });

  it('a motion bar is its own row and legend key', () => {
    const m = presenceMark('motion', [T0, T0 + 20, 1]);
    expect(m.row).toBe('motion');
    expect(m.activity).toBe('presence:motion');
    expect(m.label).toBe('Motion');
  });
});

describe('clipToFootage — no marker without footage', () => {
  const ms = (s: number) => (T0 + s) * 1000;
  const footage: [number, number][] = [[ms(0), ms(100)], [ms(200), ms(300)]];   // gap 100–200

  it('drops a bar that lies entirely in a recording gap', () => {
    expect(clipToFootage([presenceMark('person', [T0 + 120, T0 + 150, 1])], footage)).toEqual([]);
  });

  it('drops a bar beyond the newest indexed footage, and one before the oldest', () => {
    expect(clipToFootage([presenceMark('motion', [T0 + 310, T0 + 320, 1])], footage)).toEqual([]);
    expect(clipToFootage([presenceMark('motion', [T0 - 50, T0 - 10, 1])], footage)).toEqual([]);
  });

  it('keeps a bar fully on footage unchanged', () => {
    const m = presenceMark('person', [T0 + 10, T0 + 40, 1]);
    expect(clipToFootage([m], footage)).toEqual([m]);
  });

  it('splits a bar that crosses a gap into its playable pieces', () => {
    const out = clipToFootage([presenceMark('person', [T0 + 90, T0 + 220, 1])], footage);
    expect(out.map(p => [p.start - T0, p.end! - T0])).toEqual([[90, 100], [200, 220]]);
    expect(new Set(out.map(p => p.id)).size).toBe(2);
    expect(out[1].seekAt).toBe(T0 + 200);                       // plays from the gap's end
  });

  it('keeps a point event only on footage', () => {
    expect(clipToFootage([mark('in', T0 + 50)], footage)).toHaveLength(1);
    expect(clipToFootage([mark('gap', T0 + 150)], footage)).toEqual([]);
  });

  it('never lets the pre-roll seek into a gap or before the recording', () => {
    // Event at 205 s wants to seek 10 s earlier (195 s) — inside the gap.
    const [m] = clipToFootage([mark('e', T0 + 205)], footage);
    expect(m.seekAt).toBe(T0 + 200);
    const [first] = clipToFootage([presenceMark('person', [T0 + 1, T0 + 5, 1])], footage);
    expect(first.seekAt).toBe(T0);                              // not T0 - 2
  });

  it('shows nothing when no footage is known', () => {
    expect(clipToFootage([presenceMark('person', [T0, T0 + 5, 1])], [])).toEqual([]);
  });
});
