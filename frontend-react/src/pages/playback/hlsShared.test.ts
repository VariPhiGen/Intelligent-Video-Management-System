/**
 * The PROGRAM-DATE-TIME arithmetic, which both playback engines depend on.
 *
 * This is the first test file in the frontend, and this is the code that
 * earned it: the single-camera player and the multi-camera tiles both map
 * media time to wall clock through these functions, and a camera's playlist
 * begins at whatever segment boundary contains the requested instant — so the
 * mapping is the only thing making a shared playhead correct across tiles whose
 * segments start up to a minute apart.
 */
import { describe, expect, it } from 'vitest';
import {
  coverageComplete, epochAt, hlsResponseError, mediaTimeFor, planSync, playlistEnd, playlistUrl,
  type SyncTile,
} from './hlsShared';

/** A fragment as hls.js reports it: media-timeline start + wall-clock PDT (ms). */
const frag = (start: number, duration: number, pdtSec: number) =>
  ({ start, duration, programDateTime: pdtSec * 1000 } as any);

// 3 x 60 s segments, contiguous, starting at epoch 1000.
const CONTIGUOUS = [frag(0, 60, 1000), frag(60, 60, 1060), frag(120, 60, 1120)];
// Same, but with a 5-minute recording gap in the middle.
const GAPPED = [frag(0, 60, 1000), frag(60, 60, 1360)];

describe('epochAt', () => {
  it('maps a position inside a fragment to wall clock', () => {
    expect(epochAt(CONTIGUOUS, 0)).toBe(1000);
    expect(epochAt(CONTIGUOUS, 30)).toBe(1030);
    expect(epochAt(CONTIGUOUS, 90)).toBe(1090);
  });

  it('reads the wall clock across a gap from the tags, not by accumulating', () => {
    // Media time 60 is the second fragment's first frame, which is 6 minutes
    // later in the real world. Summing durations would say 1060.
    expect(epochAt(GAPPED, 60)).toBe(1360);
  });

  it('returns null past the end rather than extrapolating', () => {
    expect(epochAt(CONTIGUOUS, 999)).toBeNull();
    expect(epochAt([], 0)).toBeNull();
  });
});

describe('mediaTimeFor', () => {
  it('finds the position of a wall-clock instant', () => {
    expect(mediaTimeFor(CONTIGUOUS, 1000)).toBe(0);
    expect(mediaTimeFor(CONTIGUOUS, 1030)).toBe(30);
    expect(mediaTimeFor(CONTIGUOUS, 1150)).toBe(150);
  });

  it('lands mid-segment, not on the boundary', () => {
    // The playlist starts at the segment CONTAINING the requested instant, so
    // seeking to it must skip into that segment — otherwise every seek silently
    // rewinds by up to a minute.
    expect(mediaTimeFor(CONTIGUOUS, 1045)).toBe(45);
  });

  it('jumps forward to the next fragment when the target is inside a gap', () => {
    // 1200 is in the hole; an operator scrubbing across a gap expects the next
    // available footage, not a failure.
    expect(mediaTimeFor(GAPPED, 1200)).toBe(60);
  });

  it('clamps just inside the last frame past the end', () => {
    const t = mediaTimeFor(CONTIGUOUS, 99999)!;
    expect(t).toBeGreaterThan(179);
    expect(t).toBeLessThan(180);
  });

  it('is null with no fragments', () => {
    expect(mediaTimeFor([], 1000)).toBeNull();
  });
});

describe('round trip', () => {
  it('epochAt and mediaTimeFor invert each other', () => {
    for (const target of [1000, 1017, 1059.5, 1060, 1130, 1179]) {
      const media = mediaTimeFor(CONTIGUOUS, target)!;
      expect(epochAt(CONTIGUOUS, media)).toBeCloseTo(target, 6);
    }
  });
});

describe('playlistEnd and coverageComplete', () => {
  it('reports the wall clock one past the last fragment', () => {
    expect(playlistEnd(CONTIGUOUS)).toBe(1180);
    expect(playlistEnd([])).toBeNull();
  });

  it('calls a contiguous playlist complete', () => {
    expect(coverageComplete(CONTIGUOUS)).toBe(true);
  });

  it('calls a playlist with a recording gap incomplete', () => {
    expect(coverageComplete(GAPPED)).toBe(false);
  });

  it('tolerates sub-second muxer rounding', () => {
    const jittered = [frag(0, 59.98, 1000), frag(59.98, 60.02, 1060.01)];
    expect(coverageComplete(jittered)).toBe(true);
  });
});

describe('playlistUrl', () => {
  it('asks for the window as ISO instants and states the client HEVC answer', () => {
    const u = playlistUrl('gate-a1b2', 1_700_000_000, true, 1800);
    expect(u).toContain('/api/nvr/hls/gate-a1b2/index.m3u8');
    expect(u).toContain('hevc=true');
    expect(u).toContain(encodeURIComponent(new Date(1_700_000_000_000).toISOString()));
    expect(u).toContain(encodeURIComponent(new Date(1_700_001_800_000).toISOString()));
  });

  it('escapes a camera name rather than splicing it into the path', () => {
    expect(playlistUrl('a/b', 0, false)).toContain('hls/a%2Fb/');
  });
});

describe('hlsResponseError', () => {
  it("rebuilds the NVR's structured 404 so one classifier handles both engines", () => {
    const e = hlsResponseError({ response: { code: 404, data: JSON.stringify({
      detail: { error: 'No segments cover the requested time window',
                recorded_range: { earliest: '2026-01-01T00:00:00Z' } } }) } });
    expect(e.status).toBe(404);
    expect(e.detail.recorded_range.earliest).toBe('2026-01-01T00:00:00Z');
    expect(e.message).toContain('No segments cover');
  });

  it('survives a non-JSON body', () => {
    const e = hlsResponseError({ response: { code: 502, data: '<html>bad gateway' } });
    expect(e.status).toBe(502);
    expect(e.message).toContain('bad gateway');
  });

  it('survives no response at all', () => {
    expect(hlsResponseError({}).status).toBe(0);
  });
});

describe('planSync — the shared playhead across tiles', () => {
  // Camera A's footage ends at epoch 1180 (three 60 s segments from 1000);
  // camera B's runs 20 s longer. Normal: each camera's last segment closes at
  // its own moment.
  const A = [frag(0, 60, 1000), frag(60, 60, 1060), frag(120, 60, 1120)];
  const B = [frag(0, 60, 1000), frag(60, 60, 1060), frag(120, 80, 1120)];
  const tile = (name: string, frags: any[], currentTime: number, ended = false): SyncTile =>
    ({ name, frags, currentTime, ended, seekable: true });

  it('pulls a drifted tile back onto the leader', () => {
    const plan = planSync([tile('a', A, 30), tile('b', B, 35)], 1);
    expect(plan.clock).toBe(1030);
    expect(plan.seeks).toEqual([{ name: 'b', mediaTime: 30 }]);
  });

  it('leaves tiles within tolerance alone', () => {
    expect(planSync([tile('a', A, 30), tile('b', B, 30.5)], 1).seeks).toEqual([]);
  });

  it('never lets an ended tile drag a still-playing one back (the end-of-footage flicker)', () => {
    // A has played out and sits on its last frame — inside its last fragment,
    // because real media ends a little short of the declared duration. B is
    // still playing 10 s later. The old loop led with A and seeked B back to
    // 1179.9 every 500 ms, which is the flicker.
    const plan = planSync([tile('a', A, 179.9, true), tile('b', B, 190)], 1);
    expect(plan.clock).toBe(1190);          // B leads
    expect(plan.seeks).toEqual([]);         // and A is not revived past its end
  });

  it('does not seek a tile past the end of its own footage', () => {
    // B leads at 1195; A's footage ended at 1180 but A has not fired ended yet.
    const plan = planSync([tile('b', B, 195), tile('a', A, 178)], 1);
    expect(plan.clock).toBe(1195);
    expect(plan.seeks).toEqual([]);
  });

  it('when every tile has ended, keeps the clock and seeks nothing', () => {
    const plan = planSync([tile('a', A, 179.9, true), tile('b', B, 199.9, true)], 1);
    expect(plan.clock).toBeCloseTo(1179.9);
    expect(plan.seeks).toEqual([]);
  });

  it('never seeks a tile that cannot seek right now', () => {
    const busy = { ...tile('b', B, 35), seekable: false };
    expect(planSync([tile('a', A, 30), busy], 1).seeks).toEqual([]);
  });

  it('leads with a tile that is moving, not one frozen on its first segment', () => {
    // A was selected first but is still buffering at 1030; B is playing at
    // 1035. The old loop led with A and yanked B back every tick.
    const stuck = { ...tile('a', A, 30), advancing: false };
    const plan = planSync([stuck, tile('b', B, 35)], 1);
    expect(plan.clock).toBe(1035);
    expect(plan.seeks).toEqual([{ name: 'a', mediaTime: 35 }]);
  });

  it('pulls no one while every tile is still buffering', () => {
    const plan = planSync([{ ...tile('a', A, 30), advancing: false },
                           { ...tile('b', B, 35), advancing: false }], 1);
    expect(plan.clock).toBe(1030);
    expect(plan.seeks).toEqual([]);
  });

  it('has no clock before any tile has fragments', () => {
    expect(planSync([tile('a', [], 0)], 1)).toEqual({ clock: null, seeks: [] });
  });
});
