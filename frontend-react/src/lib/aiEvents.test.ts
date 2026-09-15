/**
 * aiEvents — the clip Open Playback plays, and the query the camera view sends.
 *
 * Open Playback is the one action every event offers, so what it plays is
 * pinned exactly: the event's camera, anchored on when the event started, with
 * the pre-roll and post-roll the API computed — not a second window rule
 * invented here.
 */
import { describe, expect, it } from 'vitest';
import { aiEvent } from '@/test/aiEvents';
import { EVENT_CLIP_MAX_SECONDS, eventClip, eventsQueryString, localInputToIso } from './aiEvents';

const STARTED = '2026-09-13T14:12:00.000Z';
const S = Date.parse(STARTED) / 1000;

describe('eventClip', () => {
  it('plays a moment as 10 s before it and 30 s from it — 40 s', () => {
    const ev = aiEvent('e1', STARTED, {
      camera: { id: 'c', slug: 'cam2-o8iu', name: 'cam2' },
      activity: { key: 'stray_parking', label: 'Stray parking', color: '#fff' },
      playback: { camera: 'cam2-o8iu', start: S - 10, end: S + 20 },
    });
    expect(eventClip(ev)).toEqual({
      cam: { slug: 'cam2-o8iu', name: 'cam2' },
      whenMs: Date.parse(STARTED),
      before: 10,
      after: 30,
      label: 'Stray parking',
    });
  });

  it('lets a short interval event run to 20 s past its end, within the same 40 s', () => {
    const ev = aiEvent('e5', STARTED, { duration_s: 5, playback: { camera: 'cam2-o8iu', start: S - 10, end: S + 25 } });
    const clip = eventClip(ev);
    expect([clip.before, clip.after]).toEqual([10, 30]);
  });

  it('caps a long interval event at 50 s', () => {
    const ev = aiEvent('e2', STARTED, {
      ended_at: new Date((S + 95) * 1000).toISOString(),
      duration_s: 95,
      playback: { camera: 'cam2-o8iu', start: S - 10, end: S + 95 + 20 },
    });
    const clip = eventClip(ev);
    expect([clip.before, clip.after]).toEqual([10, 40]);
    expect(clip.before + clip.after).toBe(EVENT_CLIP_MAX_SECONDS);
  });

  it('never follows a long playback range from the API', () => {
    const ev = aiEvent('e4', STARTED, { playback: { camera: 'cam2-o8iu', start: S - 3600, end: S + 10 * 3600 } });
    const clip = eventClip(ev);
    expect([clip.before, clip.after]).toEqual([10, 30]);
  });
});

describe('eventsQueryString', () => {
  it('sends only the filters that are set', () => {
    expect(eventsQueryString({ camera: 'gate-a1b2', limit: 50 })).toBe('?camera=gate-a1b2&limit=50');
    expect(eventsQueryString({})).toBe('');
  });

  it('carries camera, activity, range and cursor together', () => {
    const q = new URLSearchParams(eventsQueryString({
      camera: 'gate-a1b2', activity: 'no_person_area',
      from: '2026-09-13T10:00:00.000Z', to: '2026-09-13T12:00:00.000Z', before: 'abc', limit: 50,
    }).slice(1));
    expect(Object.fromEntries(q)).toEqual({
      camera: 'gate-a1b2', activity: 'no_person_area',
      from: '2026-09-13T10:00:00.000Z', to: '2026-09-13T12:00:00.000Z', before: 'abc', limit: '50',
    });
  });
});

describe('localInputToIso', () => {
  it('turns a datetime-local value into an instant with a timezone', () => {
    expect(localInputToIso('2026-09-13T14:30')).toBe(new Date('2026-09-13T14:30').toISOString());
  });

  it('treats an empty or broken value as no filter', () => {
    expect(localInputToIso('')).toBeUndefined();
    expect(localInputToIso('not a date')).toBeUndefined();
  });
});
