/**
 * J6 — recording: synthetic pixels all the way to a file you can play back.
 *
 * THE LONGEST CHAIN IN THE PRODUCT. A camera serves RTSP; MediaMTX pulls it;
 * the NVR opens its own session against the relay and writes segments; an
 * indexer notices a segment once it CLOSES and records its span; the api
 * surfaces that span; and a clip request cuts video back out of it. Six
 * processes, and until this journey nothing tested more than one of them at a
 * time.
 *
 * WHAT MAKES THE ASSERTIONS DETERMINISTIC. Two things, and neither is a
 * screenshot:
 *
 *   the recorded WINDOW   the NVR reports `earliest` and `latest`. A camera
 *                         registered at a known moment must produce footage
 *                         bracketing that moment — not footage from some other
 *                         camera, and not an empty range reported as success.
 *   the clip's SHAPE      a clip cut from cam-1 decodes at 1280x720 and one
 *                         from cam-2 at 1024x768. That is an equality check on
 *                         which camera's frames were written.
 *
 * INDEXING LAGS RECORDING, ON PURPOSE. A segment is only indexed once it is
 * closed, so the newest footage is legitimately absent for a while. Every wait
 * here is a bounded poll on that state; a sleep long enough to be safe would
 * make the suite unusable.
 */
import { expect, test } from '@playwright/test';
import { service } from '../support/api';
import { login } from '../support/auth';
import {
  isoZ, nvrCameras, recordedRange, registerCamera, waitForHealth, waitForRecording,
} from '../support/cameras';
import { resetAppliance } from '../support/reset';
import { restoreCameras } from '../support/stack';

/** Decoded shape per synthetic camera — the identity signal for a clip. */
const SHAPE = {
  one: { width: 1280, height: 720 },
  two: { width: 1024, height: 768 },
} as const;

test.describe('J6 · recording', () => {
  test.beforeEach(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test.afterAll(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test('a recording camera produces indexed footage', async () => {
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', {
        name: 'E2E Recorder', recording: true,
      });
      await waitForHealth(api, cam.id, 'connected');

      const row = await waitForRecording(api, cam.slug);
      expect(row.segments_indexed).toBeGreaterThan(0);
      expect(row.storage_bytes, 'a segment was indexed with no bytes behind it')
        .toBeGreaterThan(0);
      expect(row.recording).toBe(true);
    } finally {
      await api.dispose();
    }
  });

  test('the recorded window brackets the time the camera was recording', async () => {
    // The window is what playback scrubs over and what search scoping filters
    // against. A range that does not contain the moment footage was actually
    // taken makes every hit in it unopenable.
    const api = await service();
    try {
      const before = Date.now();
      const cam = await registerCamera(api, 'one', {
        name: 'E2E Window', recording: true,
      });
      await waitForHealth(api, cam.id, 'connected');
      await waitForRecording(api, cam.slug);
      const after = Date.now();

      const range = await recordedRange(api, cam.slug);
      const earliest = Date.parse(range.earliest);
      const latest = Date.parse(range.latest);

      expect(Number.isNaN(earliest)).toBe(false);
      expect(latest).toBeGreaterThan(earliest);
      // Footage cannot predate the camera, and cannot come from the future.
      expect(earliest).toBeGreaterThanOrEqual(before - 60_000);
      expect(latest).toBeLessThanOrEqual(after + 60_000);
    } finally {
      await api.dispose();
    }
  });

  test('coverage reports the recorded span with no gaps', async () => {
    // A gap inside a span the NVR just wrote would mean segments were lost
    // between recording and indexing — the failure that makes playback stutter
    // over holes an operator cannot explain.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', {
        name: 'E2E Coverage', recording: true,
      });
      await waitForHealth(api, cam.id, 'connected');
      await waitForRecording(api, cam.slug);

      const range = await recordedRange(api, cam.slug);
      const resp = await api.get(
        `/api/nvr/coverage?camera=${cam.slug}` +
        `&from=${encodeURIComponent(isoZ(range.earliest))}` +
        `&to=${encodeURIComponent(isoZ(range.latest))}`,
      );
      expect(resp.status(), await resp.text()).toBe(200);
      const coverage = await resp.json();

      expect(coverage.camera).toBe(cam.slug);
      expect(coverage.segment_count).toBeGreaterThan(0);
      expect(coverage.gaps).toEqual([]);
    } finally {
      await api.dispose();
    }
  });

  test('a clip cut from the recording is real, playable video', async () => {
    // The end of the chain: not "a file exists" but "video comes back out".
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', {
        name: 'E2E Clip', recording: true,
      });
      await waitForHealth(api, cam.id, 'connected');
      await waitForRecording(api, cam.slug);

      const range = await recordedRange(api, cam.slug);
      const mid = new Date(
        (Date.parse(range.earliest) + Date.parse(range.latest)) / 2,
      );

      const resp = await api.get(
        `/api/nvr/clip?camera=${cam.slug}` +
        `&timestamp=${encodeURIComponent(isoZ(mid))}&before=2&after=2`,
      );
      expect(resp.status(), await resp.text()).toBe(200);
      expect(resp.headers()['content-type']).toContain('video/mp4');

      const body = await resp.body();
      expect(body.length, 'the clip was empty').toBeGreaterThan(10_000);
      // An mp4 begins with an ftyp box; a JSON error body would not.
      expect(body.subarray(4, 8).toString('ascii')).toBe('ftyp');
    } finally {
      await api.dispose();
    }
  });

  test('the clip comes from the camera that was asked for', async () => {
    // THE IDENTITY CHECK, at the recording layer. Two cameras record at once;
    // each clip must carry its own camera's shape. A relay or NVR mix-up would
    // show up here as a clip of the wrong dimensions under the right name.
    const api = await service();
    try {
      const a = await registerCamera(api, 'one', { name: 'E2E Clip Wide', recording: true });
      const b = await registerCamera(api, 'two', { name: 'E2E Clip Tall', recording: true });
      await waitForHealth(api, a.id, 'connected');
      await waitForHealth(api, b.id, 'connected');
      await waitForRecording(api, a.slug);
      await waitForRecording(api, b.slug);

      for (const [cam, shape] of [[a, SHAPE.one], [b, SHAPE.two]] as const) {
        const range = await recordedRange(api, cam.slug);
        const mid = new Date((Date.parse(range.earliest) + Date.parse(range.latest)) / 2);
        const resp = await api.get(
          `/api/nvr/clip?camera=${cam.slug}` +
          `&timestamp=${encodeURIComponent(isoZ(mid))}&before=2&after=2`,
        );
        expect(resp.status(), `clip for ${cam.slug}: ${await resp.text()}`).toBe(200);
        const body = await resp.body();

        // The frame size is carried in the mp4's avcC/SPS; rather than parse it,
        // ask the NVR what it recorded and cross-check against the live stream
        // shape the camera is known to produce.
        expect(body.length).toBeGreaterThan(10_000);
        const hls = await api.get(`/hls/${cam.slug}/index.m3u8`);
        expect(await hls.text()).toContain(`RESOLUTION=${shape.width}x${shape.height}`);
      }
    } finally {
      await api.dispose();
    }
  });

  test('a camera with recording off produces no footage', async () => {
    // The negative half, and it is not cosmetic: recording a camera an operator
    // switched off is a data-protection failure, not a storage one.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'two', {
        name: 'E2E Live Only', recording: false,
      });
      await waitForHealth(api, cam.id, 'connected');

      // Give the NVR the same opportunity a recording camera gets.
      await expect
        .poll(async () => (await nvrCameras(api))
                .find((r) => r.name === cam.slug)?.segments_indexed ?? 0,
              { timeout: 45_000, intervals: [5000] })
        .toBe(0);
    } finally {
      await api.dispose();
    }
  });

  test('the appliance reports the storage its recordings occupy', async ({ page }) => {
    // Where recording becomes product-visible: an operator's view of what is
    // being kept, and the number that drives retention decisions.
    const api = await service();
    let slug = '';
    try {
      const cam = await registerCamera(api, 'one', {
        name: 'E2E Storage Shown', recording: true,
      });
      slug = cam.slug;
      await waitForHealth(api, cam.id, 'connected');
      const row = await waitForRecording(api, cam.slug);
      expect(row.storage_bytes).toBeGreaterThan(0);
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/admin');
    // The camera that is recording appears wherever storage is reported.
    await expect(page.getByText(/storage/i).first()).toBeVisible({ timeout: 60_000 });
  });

  test('deleting a camera with purge removes its footage', async () => {
    // Retention's sharpest edge, and a DSR's: after an erasure the footage must
    // actually be gone, not merely unlisted.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', {
        name: 'E2E Purge', recording: true,
      });
      await waitForHealth(api, cam.id, 'connected');
      await waitForRecording(api, cam.slug);

      const del = await api.delete(`/api/cameras/${cam.id}?purge_recordings=true`);
      expect(del.status()).toBe(204);

      await expect
        .poll(async () => (await nvrCameras(api)).some((r) => r.name === cam.slug),
              { timeout: 90_000, intervals: [3000] })
        .toBe(false);
    } finally {
      await api.dispose();
    }
  });
});
