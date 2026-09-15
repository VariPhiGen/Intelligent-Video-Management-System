/**
 * J4 — camera status: what the product says when a camera works, and when it stops.
 *
 * WHY THIS NEEDS A REAL STACK. The unit suite proves the health state machine
 * transitions correctly given an input. It cannot produce the input: "the
 * camera lost power" is a socket closing between two containers, noticed by
 * MediaMTX, reported to the api's health monitor on its own schedule, and
 * rendered by a page that polls. Four processes, and the interesting failures
 * are all at the joints.
 *
 * THE HONEST-UI RULE IS THE POINT OF THE SECOND HALF. A camera that has gone
 * away must say so. The failure this guards against is not a crash — it is a
 * tile that keeps showing the last good frame, or a status that stays
 * "connected" because nothing propagated. Either reads to an operator as a
 * working camera, which is the one thing this product must never claim.
 *
 * The intervention is `docker stop` on the camera container, because that is
 * what a real camera leaving the network looks like: the socket closes. A
 * paused container would keep its connections open and the product would have
 * nothing to notice.
 */
import { expect, test } from '@playwright/test';
import { service } from '../support/api';
import { login } from '../support/auth';
import {
  cameraHealth, cameraRow, registerCamera, waitForHealth, waitForReadySince,
} from '../support/cameras';
import { CAMERAS } from '../support/env';
import { resetAppliance } from '../support/reset';
import { restoreCameras, startCamera, stopCamera } from '../support/stack';

test.describe('J4 · camera status', () => {
  test.beforeEach(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test.afterAll(async () => {
    // A journey that stops a camera restores it itself; this is the safety net
    // for one that failed partway through and never got there.
    await restoreCameras();
    await resetAppliance();
  });

  // ── A camera that works ──────────────────────────────────────────────────

  test('a registered camera reaches connected, with the tracks it really has', async () => {
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Status OK' });
      await waitForHealth(api, cam.id, 'connected');

      const health = await cameraHealth(api, cam.id);
      expect(health.connected).toBe(true);
      // H264 is not assumed — it is what the synthetic camera actually encodes,
      // read back off the live session. A status that said "connected" with no
      // tracks would be a session open on nothing.
      expect(health.tracks).toContain('H264');
      expect(health.source_type).toBe('rtspSource');
    } finally {
      await api.dispose();
    }
  });

  test('a connected camera reports when its stream became ready', async () => {
    // `ready_since` is the true "up since" an operator reads. It is set from
    // MediaMTX's readyTime rather than from when the row was written, which is
    // the difference between "we asked for this camera" and "it is streaming".
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Ready Since' });
      await waitForHealth(api, cam.id, 'connected');

      const readySince = await waitForReadySince(api, cam.id);
      expect(Number.isNaN(Date.parse(readySince))).toBe(false);
      expect((await cameraRow(api, cam.id)).last_seen_at).toBeTruthy();
    } finally {
      await api.dispose();
    }
  });

  test('the inventory shows a working camera as connected', async ({ page }) => {
    const api = await service();
    let slug = '';
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Visible OK' });
      slug = cam.slug;
      await waitForHealth(api, cam.id, 'connected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/cameras');
    await expect(page.getByText('E2E Visible OK').first()).toBeVisible({ timeout: 60_000 });

    // "ONLINE", NOT "connected". The inventory maps health_status through
    // STATUS_META — connected renders as Online, disconnected as Offline — so
    // asserting on the API's word would miss the mapping being wrong, which is
    // the thing an operator actually sees.
    //
    // SCOPED TO THE CAMERA'S ROW, because the status FILTER above the table
    // contains <option>Online</option>. A page-wide getByText('Online') finds
    // that hidden option first and then fails on visibility — reporting a
    // missing status for a camera whose row says Online perfectly clearly.
    await expect(page.getByRole('row', { name: /E2E Visible OK/ }))
      .toContainText('Online', { timeout: 60_000 });
  });

  // ── A camera that goes away ──────────────────────────────────────────────

  test('a camera that loses its stream is reported as disconnected', async () => {
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Goes Away' });
      await waitForHealth(api, cam.id, 'connected');

      await stopCamera(CAMERAS.one.container);
      await waitForHealth(api, cam.id, 'disconnected');

      const health = await cameraHealth(api, cam.id);
      expect(health.connected).toBe(false);
      // No tracks, because there is no session. A status that flipped while
      // the track list stayed populated would be reporting stale detail.
      expect(health.tracks).toEqual([]);
    } finally {
      await startCamera(CAMERAS.one.container);
      await api.dispose();
    }
  });

  test('a disconnected camera stops claiming an uptime', async () => {
    // THE HONEST-UI RULE, in its sharpest form. `ready_since` is what the UI
    // renders as "up since"; leaving the old value in place would show a
    // camera that has been down for an hour as having been up all along.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E No False Uptime' });
      await waitForHealth(api, cam.id, 'connected');
      await waitForReadySince(api, cam.id);

      await stopCamera(CAMERAS.one.container);
      await waitForHealth(api, cam.id, 'disconnected');

      expect((await cameraRow(api, cam.id)).ready_since,
        'a disconnected camera still reported a ready time').toBeNull();
    } finally {
      await startCamera(CAMERAS.one.container);
      await api.dispose();
    }
  });

  test('the appliance counts its attempts to get the camera back', async () => {
    // A camera that is down and NOT being retried is a different fault from one
    // that is down and being retried; the count is how an operator tells them
    // apart, and how a flapping camera is distinguishable from a dead one.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Reconnects' });
      await waitForHealth(api, cam.id, 'connected');
      expect((await cameraHealth(api, cam.id)).reconnect_count).toBe(0);

      await stopCamera(CAMERAS.one.container);
      await waitForHealth(api, cam.id, 'disconnected');

      await expect
        .poll(async () => (await cameraHealth(api, cam.id)).reconnect_count,
              { timeout: 120_000, intervals: [3000] })
        .toBeGreaterThan(0);
    } finally {
      await startCamera(CAMERAS.one.container);
      await api.dispose();
    }
  });

  test('the camera comes back on its own once the stream returns', async () => {
    // Nobody presses anything. An appliance that needs a human to reconnect a
    // camera after a power blip is an appliance that is down all weekend.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Recovers' });
      await waitForHealth(api, cam.id, 'connected');

      await stopCamera(CAMERAS.one.container);
      await waitForHealth(api, cam.id, 'disconnected');

      await startCamera(CAMERAS.one.container);
      await waitForHealth(api, cam.id, 'connected');

      const health = await cameraHealth(api, cam.id);
      expect(health.connected).toBe(true);
      expect(health.tracks).toContain('H264');
      await waitForReadySince(api, cam.id);
    } finally {
      await restoreCameras();
      await api.dispose();
    }
  });

  test('the inventory shows a stopped camera as not connected', async ({ page }) => {
    const api = await service();
    let slug = '';
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Visible Down' });
      slug = cam.slug;
      await waitForHealth(api, cam.id, 'connected');
      await stopCamera(CAMERAS.one.container);
      await waitForHealth(api, cam.id, 'disconnected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/cameras');
    await expect(page.getByText('E2E Visible Down').first()).toBeVisible({ timeout: 60_000 });
    // The operator must be told, in the product's own vocabulary: disconnected
    // renders as "Offline". Row-scoped for the same reason as above.
    const row = page.getByRole('row', { name: /E2E Visible Down/ });
    await expect(row).toContainText('Offline', { timeout: 60_000 });
    await expect(row).not.toContainText('Online');

    await startCamera(CAMERAS.one.container);
  });

  // ── A camera that was never there ────────────────────────────────────────

  test('a camera pointed at nothing never claims to be connected', async () => {
    // Registration succeeds — the product does not require a camera to be
    // reachable to be registered, which is right: an installer configures ahead
    // of the electrician. What it must never do is report it as working.
    const api = await service();
    try {
      const resp = await api.post('/api/cameras', {
        data: {
          name: 'E2E Nowhere',
          // A host that exists on the bridge but serves no RTSP.
          rtsp_url: 'rtsp://vms-e2e-onvif-sim:8554/does-not-exist',
          lawful_basis: 'Public safety / State function',
          purpose: 'E2E unreachable camera',
          recording: false,
        },
      });
      expect(resp.status()).toBe(201);
      const cam = await resp.json();

      // Give the health monitor several cycles; it must never say connected.
      await expect
        .poll(async () => (await cameraRow(api, cam.id)).health_status,
              { timeout: 60_000, intervals: [3000] })
        .not.toBe('connected');

      const health = await cameraHealth(api, cam.id);
      expect(health.connected).toBe(false);
      expect(health.tracks).toEqual([]);
    } finally {
      await api.dispose();
    }
  });

  test('two cameras report their own status independently', async () => {
    // A single shared health flag would make one camera going down look like an
    // outage, and one coming back look like a recovery that never happened.
    const api = await service();
    try {
      const a = await registerCamera(api, 'one', { name: 'E2E Independent A' });
      const b = await registerCamera(api, 'two', { name: 'E2E Independent B' });
      await waitForHealth(api, a.id, 'connected');
      await waitForHealth(api, b.id, 'connected');

      await stopCamera(CAMERAS.one.container);
      await waitForHealth(api, a.id, 'disconnected');

      // B is untouched and must stay untouched.
      expect((await cameraRow(api, b.id)).health_status).toBe('connected');
      expect((await cameraHealth(api, b.id)).tracks).toContain('H264');
    } finally {
      await restoreCameras();
      await api.dispose();
    }
  });
});
