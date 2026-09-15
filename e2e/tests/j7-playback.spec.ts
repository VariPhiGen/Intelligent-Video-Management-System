/**
 * J7 — playback: the footage you asked for is the footage you get.
 *
 * THE CLAIM THIS JOURNEY MAKES, and no other test in the repository can. Ask
 * for 13:48:35 on this camera, and the frames that come back really were taken
 * at 13:48:35 by that camera. Everything below this level can only check that
 * some bytes were returned and that a timestamp was echoed in a response.
 *
 * HOW IT IS PROVEN. The synthetic camera burns the wall-clock epoch into every
 * frame, and e2e/fixtures/rtsp-cam/timecode.py reads it back by matching glyphs
 * against a reference the fixture renders for itself. A clip requested at T
 * with `before=6&after=6` therefore has to decode to a run of seconds
 * containing exactly T. The window is ±6 rather than ±2 because the burned-in
 * clock runs ~2 s behind the NVR's timeline — see the first test for the
 * measurement. That is an equality, not a similarity — which is why OCR was
 * rejected for the job (tesseract read a clean `EPOCH 1789055315` as
 * `01789055312`).
 *
 * WHAT A FAILURE HERE WOULD MEAN. Off-by-one segment selection, a timezone
 * slip between the NVR's index and its cutter, or a clip served from the wrong
 * camera — none of which produce an error, all of which produce video. On a
 * forensic product, footage confidently served from the wrong minute is worse
 * than no footage at all.
 */
import { expect, test } from '@playwright/test';
import { service } from '../support/api';
import {
  isoZ, recordedRange, registerCamera, waitForHealth, waitForRecordedSpan,
  waitForRecording,
} from '../support/cameras';
import { resetAppliance } from '../support/reset';
import { restoreCameras } from '../support/stack';
import { decodeTimecodes, describeWindow, readableEpochs } from '../support/timecode';

/** Recording, indexing and cutting are each bounded polls; the whole chain is
 *  slower than a UI journey and this is the budget it needs. */
test.describe.configure({ timeout: 300_000 });

test.describe('J7 · playback', () => {
  test.beforeEach(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test.afterAll(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  /** A camera that has been recording long enough to cut a clip out of. */
  async function recordingCamera(api: any, name: string, which: 'one' | 'two' = 'one') {
    const cam = await registerCamera(api, which, { name, recording: true });
    await waitForHealth(api, cam.id, 'connected');
    await waitForRecording(api, cam.slug);
    return cam;
  }

  test('a clip contains the exact second it was asked for', async () => {
    // THE CENTRAL ASSERTION OF THE JOURNEY.
    const api = await service();
    try {
      const cam = await recordingCamera(api, 'E2E Playback Exact');

      // NOT THE MIDPOINT OF WHATEVER EXISTS YET. `waitForRecording` returns at
      // the FIRST indexed segment, so the range can be only a few seconds long
      // — and the midpoint of a short range sits inside the newest footage,
      // which the recorder has published a range for but cannot yet CUT. The
      // cutter then clamps backwards and serves earlier seconds: observed as
      // "asked for 1789108902; the clip covered ...899, 900, 901", with the
      // requested second one past the end of the clip.
      //
      // Intermittent by nature — it depends on how much footage happens to
      // exist when the test runs — which is exactly why it has to be waited
      // for rather than hoped for. Same defect class as the distinct-moments
      // test further down, and the same fix.
      const range = await waitForRecordedSpan(api, cam.slug, 20);
      const start = Math.floor(Date.parse(range.earliest) / 1000);
      // Comfortably inside: clear of `earliest` (grooming trims there) and
      // well clear of the un-cuttable tail at `latest`.
      const requested = start + 8;

      // ±6, NOT ±2, AND THE REASON IS A MEASURED PROPERTY OF THE PIPELINE.
      //
      // The burned-in timecode is the CAMERA's clock at capture. The NVR's
      // timeline is when the frame ARRIVED to be written. Between them sit the
      // encoder, the relay and the segment writer, and that gap is real:
      // measured on this appliance at ~2 s (a clip requested at T came back
      // centred on T-2, with `before=6&after=6` covering T-7..T+3).
      //
      // A ±2 s window is narrower than that offset, so the requested second
      // fell off the end of the clip — observed twice as "asked for X; the
      // clip covered X-3 .. X-1". Nothing was wrong with the product: the
      // window was too small to contain the answer.
      //
      // WHAT THIS TEST CAN AND CANNOT RESOLVE. It catches the errors that
      // matter and are catchable — a segment selected minutes or hours away, a
      // timezone slip, footage from the wrong camera. It CANNOT resolve a
      // cutter skew smaller than the pipeline latency, because a constant
      // offset of a couple of seconds is exactly what the pipeline already
      // contributes. Making that claim would need the capture and record
      // clocks tied together, which this fixture does not do.
      const resp = await api.get(
        `/api/nvr/clip?camera=${cam.slug}` +
        `&timestamp=${encodeURIComponent(isoZ(new Date(requested * 1000)))}` +
        `&before=6&after=6`,
      );
      expect(resp.status(), await resp.text()).toBe(200);

      const decoded = await decodeTimecodes(await resp.body());
      const epochs = readableEpochs(decoded);
      expect(epochs.length, 'no frame carried a readable timecode').toBeGreaterThan(0);

      expect(epochs,
        `asked for ${requested}; the clip covered ${describeWindow(epochs)}`,
      ).toContain(requested);

      // And the clip really is BUILT around the request rather than happening
      // to overlap it: its centre sits within the pipeline's own latency of
      // the moment asked for. A clip that merely brushed the requested second
      // would pass the assertion above.
      const centre = epochs[Math.floor(epochs.length / 2)];
      expect(Math.abs(centre - requested),
        `the clip is centred on ${centre}, ${Math.abs(centre - requested)}s from `
        + `the requested ${requested} — further than the pipeline can explain`,
      ).toBeLessThanOrEqual(5);
    } finally {
      await api.dispose();
    }
  });

  test('the clip window matches the before/after that was requested', async () => {
    // `before` and `after` are what a scrub bar turns a drag into. If they are
    // not honoured, the operator's window and the footage silently disagree.
    const api = await service();
    try {
      const cam = await recordingCamera(api, 'E2E Playback Window');
      // Same wait as the exact-second test above, and for the same reason: a
      // moment derived from a range that is only seconds long lands in footage
      // the recorder has published but cannot yet cut. `after=3` reaches even
      // further into that tail than `after=2` does.
      const range = await waitForRecordedSpan(api, cam.slug, 20);
      const requested = Math.floor(Date.parse(range.earliest) / 1000) + 8;

      const resp = await api.get(
        `/api/nvr/clip?camera=${cam.slug}` +
        `&timestamp=${encodeURIComponent(isoZ(new Date(requested * 1000)))}` +
        `&before=3&after=3`,
      );
      expect(resp.status()).toBe(200);

      const epochs = readableEpochs(await decodeTimecodes(await resp.body()));
      const first = Math.min(...epochs);
      const last = Math.max(...epochs);

      // Bounds rather than exact equality: the cutter aligns to keyframes, so
      // the clip may start slightly early. What it must never do is start
      // AFTER the requested moment or end before it.
      expect(first, `clip started at ${first}, after the requested ${requested}`)
        .toBeLessThanOrEqual(requested);
      expect(last, `clip ended at ${last}, before the requested ${requested}`)
        .toBeGreaterThanOrEqual(requested);
      expect(last - first).toBeLessThanOrEqual(12);
    } finally {
      await api.dispose();
    }
  });

  test('the timecode advances one second per second of footage', async () => {
    // Proves the clip is real elapsed time rather than a repeated frame or a
    // stall — and that `-re` on the fixture is doing its job, so every other
    // timing assertion in this file rests on something.
    const api = await service();
    try {
      const cam = await recordingCamera(api, 'E2E Playback Monotonic');
      // Same wait as the exact-second test above, and for the same reason: a
      // moment derived from a range that is only seconds long lands in footage
      // the recorder has published but cannot yet cut. `after=3` reaches even
      // further into that tail than `after=2` does.
      const range = await waitForRecordedSpan(api, cam.slug, 20);
      const requested = Math.floor(Date.parse(range.earliest) / 1000) + 8;

      const resp = await api.get(
        `/api/nvr/clip?camera=${cam.slug}` +
        `&timestamp=${encodeURIComponent(isoZ(new Date(requested * 1000)))}` +
        `&before=3&after=3`,
      );
      const epochs = readableEpochs(await decodeTimecodes(await resp.body()));
      expect(epochs.length).toBeGreaterThanOrEqual(3);

      for (let i = 1; i < epochs.length; i += 1) {
        expect(epochs[i], `timecode went backwards: ${epochs.join(', ')}`)
          .toBeGreaterThanOrEqual(epochs[i - 1]);
        expect(epochs[i] - epochs[i - 1],
          `a second of footage jumped ${epochs[i] - epochs[i - 1]}s`)
          .toBeLessThanOrEqual(2);
      }
    } finally {
      await api.dispose();
    }
  });

  test('two different requested moments return different footage', async () => {
    // The strongest guard against a cutter that ignores its timestamp and
    // always returns the same segment: two windows, two distinct sets of
    // seconds. A single-window test cannot tell the difference.
    //
    // THE WINDOW WIDTH AND THE SPACING ARE BOTH LOAD-BEARING, and the first
    // version of this test got them wrong. A ±1s clip is about two seconds of
    // footage; sampled once per second, and started early because the cutter
    // aligns to a keyframe, it can decode to a SINGLE frame one second before
    // the moment requested — which is what happened (asked 1789055824, read
    // [1789055823]). That is the sampling resolution talking, not the product:
    // the exactness claim belongs to the first test in this file, which uses a
    // window wide enough to make it. Here the window only has to be wide enough
    // to read reliably, and the two windows far enough apart that overlapping
    // would mean the cutter genuinely ignored the timestamp.
    const api = await service();
    try {
      const cam = await recordingCamera(api, 'E2E Playback Distinct');

      // ±2s windows span ~5s each, plus keyframe slop. Sampling 30s apart
      // leaves no honest way for the two to touch.
      const SPACING = 30;
      const range = await waitForRecordedSpan(api, cam.slug, SPACING + 8);
      const start = Math.floor(Date.parse(range.earliest) / 1000);
      const end = Math.floor(Date.parse(range.latest) / 1000);

      const early = start + 4;
      const late = early + SPACING;
      expect(late, 'not enough footage to sample two moments')
        .toBeLessThanOrEqual(end - 4);

      const clipAt = async (t: number) => {
        const r = await api.get(
          `/api/nvr/clip?camera=${cam.slug}` +
          `&timestamp=${encodeURIComponent(isoZ(new Date(t * 1000)))}` +
          `&before=2&after=2`,
        );
        expect(r.status(), await r.text()).toBe(200);
        return readableEpochs(await decodeTimecodes(await r.body()));
      };

      const a = await clipAt(early);
      const b = await clipAt(late);
      expect(a.length, 'the early clip carried no readable timecode').toBeGreaterThan(0);
      expect(b.length, 'the late clip carried no readable timecode').toBeGreaterThan(0);

      // Each clip landed near what was asked for...
      expect(Math.abs(Math.min(...a) - early),
        `asked for ${early}; the clip covered ${describeWindow(a)}`).toBeLessThanOrEqual(4);
      expect(Math.abs(Math.min(...b) - late),
        `asked for ${late}; the clip covered ${describeWindow(b)}`).toBeLessThanOrEqual(4);

      // ...and they are genuinely different footage, which is the point.
      expect(a.some((e) => b.includes(e)),
        `the two windows overlapped: ${a.join(',')} vs ${b.join(',')}`).toBe(false);
    } finally {
      await api.dispose();
    }
  });

  test('a clip from one camera never contains another camera\'s footage', async () => {
    // Both cameras record the same wall clock, so the timecode cannot tell them
    // apart — the frame SHAPE can. cam-1 is 1280x720 and cam-2 is 1024x768.
    const api = await service();
    try {
      const a = await recordingCamera(api, 'E2E Playback Cam A', 'one');
      const b = await recordingCamera(api, 'E2E Playback Cam B', 'two');

      for (const [cam, width] of [[a, 1280], [b, 1024]] as const) {
        const range = await recordedRange(api, cam.slug);
        const mid = Math.floor(
          (Date.parse(range.earliest) + Date.parse(range.latest)) / 2 / 1000);
        const resp = await api.get(
          `/api/nvr/clip?camera=${cam.slug}` +
          `&timestamp=${encodeURIComponent(isoZ(new Date(mid * 1000)))}` +
          `&before=1&after=1`,
        );
        expect(resp.status()).toBe(200);
        const hls = await api.get(`/hls/${cam.slug}/index.m3u8`);
        expect(await hls.text(),
          `${cam.slug} is not serving ${width}-wide video`).toContain(`RESOLUTION=${width}x`);
      }
    } finally {
      await api.dispose();
    }
  });

  // ── Boundaries ───────────────────────────────────────────────────────────

  test('asking for a moment before the recording begins returns no clip', async () => {
    // The honest answer to "there is no footage there" is a refusal, not an
    // empty file and not the nearest thing the cutter could find. An operator
    // handed the wrong minute has no way to know.
    const api = await service();
    try {
      const cam = await recordingCamera(api, 'E2E Playback Before');
      const range = await recordedRange(api, cam.slug);
      const wayBefore = new Date(Date.parse(range.earliest) - 3 * 3600 * 1000);

      const resp = await api.get(
        `/api/nvr/clip?camera=${cam.slug}` +
        `&timestamp=${encodeURIComponent(isoZ(wayBefore))}&before=2&after=2`,
      );
      expect(resp.status(),
        `expected a refusal for a time outside the recording; got ${resp.status()}`)
        .toBeGreaterThanOrEqual(400);
    } finally {
      await api.dispose();
    }
  });

  test('asking for a moment in the future returns no clip', async () => {
    const api = await service();
    try {
      const cam = await recordingCamera(api, 'E2E Playback Future');
      const future = new Date(Date.now() + 3 * 3600 * 1000);
      const resp = await api.get(
        `/api/nvr/clip?camera=${cam.slug}` +
        `&timestamp=${encodeURIComponent(isoZ(future))}&before=2&after=2`,
      );
      expect(resp.status()).toBeGreaterThanOrEqual(400);
    } finally {
      await api.dispose();
    }
  });

  test('a camera that never recorded has no footage to play', async () => {
    // Registered, streaming, recording off. The distinction between "nothing
    // was recorded" and "something went wrong" is the whole point.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'two', {
        name: 'E2E Playback None', recording: false,
      });
      await waitForHealth(api, cam.id, 'connected');

      const resp = await api.get(
        `/api/nvr/clip?camera=${cam.slug}` +
        `&timestamp=${encodeURIComponent(isoZ(new Date()))}&before=2&after=2`,
      );
      expect(resp.status()).toBeGreaterThanOrEqual(400);
    } finally {
      await api.dispose();
    }
  });

  test('an unknown camera is refused rather than served something', async () => {
    const api = await service();
    try {
      const resp = await api.get(
        `/api/nvr/clip?camera=no-such-camera-9999` +
        `&timestamp=${encodeURIComponent(isoZ(new Date()))}&before=2&after=2`,
      );
      expect(resp.status()).toBeGreaterThanOrEqual(400);
    } finally {
      await api.dispose();
    }
  });

  test('coverage tells an operator where the footage actually is', async () => {
    // What the scrub bar draws. A window that claims coverage it does not have
    // sends an operator to a dead spot; one that hides coverage it does have
    // loses evidence.
    const api = await service();
    try {
      const cam = await recordingCamera(api, 'E2E Playback Coverage');
      const range = await recordedRange(api, cam.slug);

      const inside = await api.get(
        `/api/nvr/coverage?camera=${cam.slug}` +
        `&from=${encodeURIComponent(isoZ(range.earliest))}` +
        `&to=${encodeURIComponent(isoZ(range.latest))}`,
      );
      expect(inside.status()).toBe(200);
      expect((await inside.json()).segment_count).toBeGreaterThan(0);

      const before = new Date(Date.parse(range.earliest) - 2 * 3600 * 1000);
      const alsoBefore = new Date(Date.parse(range.earliest) - 1 * 3600 * 1000);
      const outside = await api.get(
        `/api/nvr/coverage?camera=${cam.slug}` +
        `&from=${encodeURIComponent(isoZ(before))}` +
        `&to=${encodeURIComponent(isoZ(alsoBefore))}`,
      );
      expect(outside.status()).toBe(200);
      expect((await outside.json()).segment_count,
        'the NVR claimed coverage for a window it never recorded').toBe(0);
    } finally {
      await api.dispose();
    }
  });
});
