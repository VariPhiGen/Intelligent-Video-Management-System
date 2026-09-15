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
  coverageComplete, epochAt, hlsResponseError, mediaTimeFor, playlistEnd, playlistUrl,
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
