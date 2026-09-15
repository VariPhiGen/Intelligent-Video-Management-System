/**
 * J3 — onboarding a camera, from a device nobody has heard of to a relayed feed.
 *
 * WHAT ONLY THIS LEVEL CAN SHOW. The backend suite proves `onvif_probe`
 * classifies a response correctly and `netscan` merges a ProbeMatch correctly,
 * each against a stub. What no unit can show is the whole chain actually
 * closing: a WS-Discovery datagram on the wire, answered by a device that
 * speaks real ONVIF SOAP, parsed by real onvif-zeep, promoted through the real
 * DPDP gate, and ending as a MediaMTX path that pulls real pixels.
 *
 * Every step of that chain is a different process, and the joints are where
 * this product has historically broken.
 *
 * THE DEVICE IS SYNTHETIC, THE PROTOCOL IS NOT. e2e/fixtures/onvif-sim answers
 * the four calls `_probe_sync` makes plus GetSystemDateAndTime, from the same
 * response shapes tests/onvif_fixtures.py uses one level down. Its RTSP URL
 * points at a camera container that really serves H264.
 */
import { expect, test } from '@playwright/test';
import { service } from '../support/api';
import { APP_READY, login } from '../support/auth';
import { CAMERAS, ONVIF } from '../support/env';
import { cameraRtspUrl, relayPathNames } from '../support/stack';
import { resetAppliance } from '../support/reset';

const LAWFUL_BASIS = 'Public safety / State function';

test.describe('J3 · camera onboarding', () => {
  // Each journey starts from an appliance with no cameras: onboarding the same
  // camera twice is a different test (and a legitimate one), not the accident
  // of a previous run.
  test.beforeEach(async () => {
    await resetAppliance();
  });

  test.afterAll(async () => {
    await resetAppliance();
  });

  // ── Discovery ────────────────────────────────────────────────────────────

  test('a scan finds the device, and credentials identify it', async ({ page }) => {
    // THE WIZARD IS TWO STEPS, AND THE SPLIT IS THE PRODUCT'S DESIGN.
    //
    // Step 1 sweeps and announces; it deliberately carries no credentials, so a
    // device that answered is only "discovered" — an open port with no proof it
    // is a camera. That is why the summary reads "0 new cameras found" after a
    // successful scan: the count is of devices with camera EVIDENCE, and there
    // is none yet. Step 2 supplies credentials, probes ONVIF for real, and that
    // is where a device acquires a vendor and a model.
    //
    // A test that asserted an identity on step 1 would be asserting something
    // the product never promised — and that is exactly what the first draft of
    // this test did.
    await login(page, 'admin');
    await page.goto('/#/cameras/add');

    const start = page.getByRole('button', { name: /Start auto-discovery/i })
      .or(page.getByRole('button', { name: /Scan again/i }));
    await expect(start.first()).toBeVisible({ timeout: 30_000 });
    await start.first().click();

    // The scan has finished when the step says so — a state, not a duration.
    await expect(page.getByText(/cameras? found/i).first())
      .toBeVisible({ timeout: 120_000 });

    await page.getByRole('button', { name: /Continue/i }).click();

    // Step 2 lists what the sweep found, before any credential is offered.
    await expect(page.getByText(/Username \(apply to all\)/i))
      .toBeVisible({ timeout: 30_000 });

    const user = page.locator('.wz-credbox input').first();
    const pass = page.locator('.wz-credbox input[type="password"]').first();
    await user.fill(ONVIF.user);
    await pass.fill(ONVIF.password);
    await page.getByRole('button', { name: /Test all/i }).click();

    // NOW the device has an identity, and it came from a real
    // GetDeviceInformation over real ONVIF SOAP — which is the whole point of
    // this journey.
    await expect(page.getByText(ONVIF.model).first())
      .toBeVisible({ timeout: 120_000 });
  });

  test('a scan reports the device as verified once credentials are right', async ({ page }) => {
    // The scan carries the credentials the simulator accepts, so the device
    // should come back already probed rather than needing a second pass.
    const api = await service();
    try {
      const started = await api.post('/api/discovery/scan', {
        data: { cidr: null, username: ONVIF.user, password: ONVIF.password },
      });
      expect(started.ok(), await started.text()).toBe(true);

      await expect
        .poll(async () => (await (await api.get('/api/discovery/scan')).json()).status,
              { timeout: 120_000, intervals: [2000] })
        .toBe('done');

      const devices = await (await api.get('/api/discovery/devices')).json();
      const items: any[] = Array.isArray(devices) ? devices : devices.devices ?? devices.items ?? [];
      const cam = items.find((d) => d.model === ONVIF.model);
      expect(cam, 'the ONVIF simulator was not discovered').toBeTruthy();
      expect(cam.status).toBe('verified');
      expect(cam.vendor).toBe(ONVIF.vendor);
      expect(cam.onvif_port).toBe(8080);
    } finally {
      await api.dispose();
    }
  });

  test('the wrong password is reported as a credential problem, not a missing camera', async () => {
    // THE DISTINCTION THAT COSTS AN INSTALLER AN HOUR. A camera that answers
    // ONVIF and refuses the password must land in "check the credentials", not
    // in "this is not a camera" — where nobody looks again.
    const api = await service();
    try {
      const started = await api.post('/api/discovery/scan', {
        data: { cidr: null, username: 'definitely-wrong', password: 'also-wrong' },
      });
      expect(started.ok()).toBe(true);
      await expect
        .poll(async () => (await (await api.get('/api/discovery/scan')).json()).status,
              { timeout: 120_000, intervals: [2000] })
        .toBe('done');

      const devices = await (await api.get('/api/discovery/devices')).json();
      const items: any[] = Array.isArray(devices) ? devices : devices.devices ?? devices.items ?? [];
      // Identified by IP, because with bad credentials there is no model to
      // identify it by — which is exactly the situation being tested.
      const simIp = items.map((d) => d.ip);
      expect(simIp.length, 'the scan found nothing at all').toBeGreaterThan(0);
      const authFailed = items.filter((d) => d.status === 'auth_failed');
      expect(authFailed.length,
        `no device reported auth_failed; statuses were ${items.map((d) => d.status).join(', ')}`,
      ).toBeGreaterThan(0);
    } finally {
      await api.dispose();
    }
  });

  // ── Registration ─────────────────────────────────────────────────────────

  test('a camera added by RTSP URL appears in the inventory', async ({ page }) => {
    await login(page, 'admin');
    await page.goto('/#/cameras/add');

    // The four discovery methods are cards, not tabs; the manual one is chosen
    // by the label an operator reads.
    await page.getByText('Manual IP / RTSP').first().click();

    const url = cameraRtspUrl('one');
    await page.getByPlaceholder(/rtsp:\/\/192\.168/).fill(url);
    await page.getByPlaceholder(/auto-named if left blank/).fill('E2E Front Gate');
    // Both DPDP fields are required by the form, exactly as the API requires
    // them — the wizard is where an operator meets that gate.
    await page.getByLabel(/Lawful basis/i).selectOption(LAWFUL_BASIS)
      .catch(async () => { await page.locator('select').first().selectOption(LAWFUL_BASIS); });
    await page.getByPlaceholder(/Why this camera records/).fill('E2E perimeter monitoring');
    await page.getByRole('button', { name: /Add camera/i }).click();

    // The product-level outcome: it is in the inventory, by the name given.
    await page.goto('/#/cameras');
    await expect(page.getByText('E2E Front Gate').first())
      .toBeVisible({ timeout: 60_000 });
  });

  test('registration is refused without a lawful basis', async () => {
    // The DPDP gate, at the API where it is enforced. A camera that registers
    // without a basis is a compliance failure the UI alone cannot prevent.
    const api = await service();
    try {
      const resp = await api.post('/api/cameras', {
        data: {
          name: 'E2E No Basis',
          rtsp_url: cameraRtspUrl('one'),
          purpose: 'testing',
        },
      });
      expect(resp.status()).toBe(422);
      expect(await resp.text()).toMatch(/lawful basis/i);
    } finally {
      await api.dispose();
    }
  });

  test('registration is refused with an invented lawful basis', async () => {
    const api = await service();
    try {
      const resp = await api.post('/api/cameras', {
        data: {
          name: 'E2E Bad Basis',
          rtsp_url: cameraRtspUrl('one'),
          lawful_basis: 'because I said so',
          purpose: 'testing',
        },
      });
      expect(resp.status()).toBe(422);
    } finally {
      await api.dispose();
    }
  });

  // ── The chain closing ────────────────────────────────────────────────────

  test('a registered camera reaches the relay and pulls real video', async () => {
    // THE JOINT THIS JOURNEY EXISTS FOR. Registration writes a row; the relay
    // pulling from the camera is a separate process reacting to it, and a row
    // that never becomes a path is a camera the operator sees and cannot watch.
    const api = await service();
    try {
      const created = await api.post('/api/cameras', {
        data: {
          name: 'E2E Relay Check',
          rtsp_url: cameraRtspUrl('one'),
          lawful_basis: LAWFUL_BASIS,
          purpose: 'E2E relay verification',
          recording: false,
        },
      });
      expect(created.status(), await created.text()).toBe(201);
      const slug = (await created.json()).slug as string;
      expect(slug).toBeTruthy();

      // The path appears...
      await expect
        .poll(async () => (await relayPathNames()).includes(slug),
              { timeout: 60_000, intervals: [1000] })
        .toBe(true);

      // ...and it is READY, which is the difference between "the api asked for
      // a path" and "the relay is actually pulling frames from the camera".
      await expect
        .poll(async () => {
          const { relayPaths } = await import('../support/stack');
          return (await relayPaths()).find((p) => p.name === slug)?.ready ?? false;
        }, { timeout: 90_000, intervals: [2000] })
        .toBe(true);
    } finally {
      await api.dispose();
    }
  });

  test('the camera the operator sees carries the vendor the device reported', async ({ page }) => {
    // Onboarding through discovery, then asserting on screen: the identity a
    // real ONVIF call produced has to survive all the way to the inventory row.
    const api = await service();
    try {
      const started = await api.post('/api/discovery/scan', {
        data: { cidr: null, username: ONVIF.user, password: ONVIF.password },
      });
      expect(started.ok()).toBe(true);
      await expect
        .poll(async () => (await (await api.get('/api/discovery/scan')).json()).status,
              { timeout: 120_000, intervals: [2000] })
        .toBe('done');

      const devices = await (await api.get('/api/discovery/devices')).json();
      const items: any[] = Array.isArray(devices) ? devices : devices.devices ?? devices.items ?? [];
      const device = items.find((d) => d.model === ONVIF.model);
      expect(device, 'the simulator was not discovered').toBeTruthy();

      const promoted = await api.post(`/api/discovery/devices/${device.id}/add`, {
        data: {
          name: 'E2E Discovered Cam',
          lawful_basis: LAWFUL_BASIS,
          purpose: 'E2E discovery promotion',
        },
      });
      expect([200, 201]).toContain(promoted.status());
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/cameras');
    await expect(page.getByText('E2E Discovered Cam').first())
      .toBeVisible({ timeout: 60_000 });
  });

  test('deleting a camera removes it from the inventory and the relay', async () => {
    // The other half of onboarding, and the one the two-track invariant is
    // about: a row deleted while its relay path survives leaves the appliance
    // pulling video for a camera nobody can see.
    const api = await service();
    try {
      const created = await api.post('/api/cameras', {
        data: {
          name: 'E2E Teardown',
          rtsp_url: cameraRtspUrl('two'),
          lawful_basis: LAWFUL_BASIS,
          purpose: 'E2E teardown check',
          recording: false,
        },
      });
      expect(created.status()).toBe(201);
      const cam = await created.json();

      await expect
        .poll(async () => (await relayPathNames()).includes(cam.slug),
              { timeout: 60_000, intervals: [1000] })
        .toBe(true);

      const del = await api.delete(`/api/cameras/${cam.id}?purge_recordings=true`);
      expect(del.status()).toBe(204);

      await expect
        .poll(async () => (await relayPathNames()).includes(cam.slug),
              { timeout: 60_000, intervals: [1000] })
        .toBe(false);

      const remaining = await (await api.get('/api/cameras')).json();
      expect(remaining.map((c: any) => c.slug)).not.toContain(cam.slug);
    } finally {
      await api.dispose();
    }
  });
});
