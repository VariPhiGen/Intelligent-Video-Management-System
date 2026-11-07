/**
 * J10 — AI is opt-in, and switching it on takes effect NOW.
 *
 * WHY THIS EARNS A JOURNEY. Three services have to agree about one camera, and
 * nothing above them can see whether they do. The registry says a camera should
 * be indexed; the frame broker has to be decoding it; the search index has to be
 * ingesting it. A contract test can pin each service's own rule — and
 * `test_new_camera_ai_default.py` and `test_ai_push_on_update.py` do — but the
 * WIRING between the three processes is only observable here, and that is where
 * both of the 2026-09-18 defects lived:
 *
 *   1. `search_indexing` defaulted TRUE, so every camera an operator onboarded
 *      silently started collecting person and vehicle crops. Registering a
 *      camera is a recording decision; indexing it is a second decision, with
 *      its own cost and — the crops being personal data — its own justification.
 *      Migration 038 made it opt-in.
 *
 *   2. Switching it on pushed the change to the search index and to NOBODY
 *      ELSE. The broker and analytics found out on their next reconcile sweep,
 *      60 s later. So the first camera an operator switched on produced nothing
 *      for over a minute; working down a fleet, results appeared around the time
 *      the LAST camera was switched on, which reads as "Smart Search needs every
 *      camera enabled".
 *
 * WHY THE ASSERTIONS ARE NOT POLLED, and this is the whole design of the file.
 * The reconcile sweep is a real backstop: given 60 s it registers the camera
 * whether or not the push exists. A polling assertion with any generous timeout
 * therefore passes against the BUG — the same trap J9 documents with the health
 * monitor's orphan sweep, where mutation-testing a deletion left the test green
 * because it sat waiting for the sweep to tidy up.
 *
 * `update_camera` awaits both pushes before it answers 200, so by the time this
 * client holds the response the services must already know. Asserting at that
 * instant is what separates "the switch reached them" from "something swept up
 * a minute later". Mutation-checked: removing the push block from
 * `update_camera` fails these tests in ~1 s.
 *
 * ANALYTICS IS ABSENT FROM THIS STACK, by profile — `COMPOSE_PROFILES=search,
 * frames` (see e2e.env). Its half of the same push is covered by
 * `test_ai_push_on_update.py`. What is covered here is the pair of services
 * that DO run, which is also the pair the operator's switch is useless without:
 * the broker supplies the frames and the index stores the result.
 */
import { expect, test } from '@playwright/test';
import { service } from '../support/api';
import { login } from '../support/auth';
import { registerCamera, waitForHealth } from '../support/cameras';
import { resetAppliance } from '../support/reset';
import {
  brokerCameras, indexCameras, indexDomains, restoreCameras,
} from '../support/stack';

test.describe.configure({ timeout: 300_000 });

/** Comfortably under the 60 s reconcile interval, so a sweep cannot be what
 *  made an assertion pass. Everything asserted here is awaited by the endpoint
 *  before it responds, so the real elapsed time is milliseconds. */
const RECONCILE_INTERVAL_MS = 60_000;

test.describe('J10 · AI is opt-in and takes effect immediately', () => {
  test.beforeEach(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test.afterAll(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  // ── The default ──────────────────────────────────────────────────────────

  test('a newly registered camera is not indexed, and no service is working on it',
    async () => {
      const api = await service();
      try {
        const cam = await registerCamera(api, 'one', { name: 'E2E AI Default Off' });
        await waitForHealth(api, cam.id, 'connected');

        const row = await (await api.get(`/api/cameras/${cam.id}`)).json();
        expect(row.search_indexing,
          'a camera must not start indexing people the moment it is registered',
        ).toBe(false);
        // The domains survive the switch being off. They are the answer to
        // "index what", not to "index", and keeping them is what makes turning
        // indexing on one click that works rather than one that appears not to.
        expect(row.search_domains.sort()).toEqual(['person', 'vehicles']);

        // The camera is CONNECTED — the relay is pulling it and the NVR could
        // record it — and still nothing is analysing it. That is the point:
        // recording and AI are separate decisions.
        expect(await brokerCameras(),
          'the broker was decoding a camera nobody switched AI on for',
        ).not.toContain(cam.slug);
        expect(await indexCameras(),
          'the index was ingesting a camera nobody switched AI on for',
        ).not.toContain(cam.slug);
      } finally {
        await api.dispose();
      }
    });

  // ── Switching it on ──────────────────────────────────────────────────────

  test('switching indexing on reaches the broker and the index before the call returns',
    async () => {
      const api = await service();
      try {
        const cam = await registerCamera(api, 'one', { name: 'E2E AI Switch On' });
        await waitForHealth(api, cam.id, 'connected');
        expect(await brokerCameras()).not.toContain(cam.slug);

        const started = Date.now();
        const put = await api.put(`/api/cameras/${cam.id}`, {
          data: { search_indexing: true },
        });
        expect(put.status(), await put.text()).toBe(200);

        // CHECKED IMMEDIATELY — see the header. A poll here would be satisfied
        // by the reconcile sweep and would pass against the defect.
        expect(await brokerCameras(),
          'THE BROKER DID NOT LEARN ABOUT THE SWITCH. It supplies the frames, '
          + 'so until it does, the index is registered to receive crops nothing '
          + 'is producing — the camera indexes nothing at all until a sweep runs',
        ).toContain(cam.slug);
        expect(await indexCameras(),
          'the index did not learn about the switch',
        ).toContain(cam.slug);

        expect(Date.now() - started,
          'the assertions above took longer than a reconcile interval, so they '
          + 'no longer prove the push happened rather than the sweep',
        ).toBeLessThan(RECONCILE_INTERVAL_MS);
      } finally {
        await api.dispose();
      }
    });

  test('the domains an operator picks are what the index is told to collect',
    async () => {
      // A domain change is not merely "on": the index holds the set per camera
      // and only stores what it is told to. Narrowing has to travel the same
      // instant path as switching on, or a camera keeps collecting a domain the
      // operator has just withdrawn consent for.
      const api = await service();
      try {
        const cam = await registerCamera(api, 'one', { name: 'E2E AI Domains' });
        await waitForHealth(api, cam.id, 'connected');
        await api.put(`/api/cameras/${cam.id}`, { data: { search_indexing: true } });
        expect(await indexDomains(cam.slug)).toEqual(['person', 'vehicles']);

        const narrowed = await api.put(`/api/cameras/${cam.id}`, {
          data: { search_domains: ['person'] },
        });
        expect(narrowed.status(), await narrowed.text()).toBe(200);
        expect(await indexDomains(cam.slug),
          'the index kept collecting vehicles after the operator withdrew them',
        ).toEqual(['person']);
      } finally {
        await api.dispose();
      }
    });

  test('switching indexing back off stops both services at once', async () => {
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E AI Switch Off' });
      await waitForHealth(api, cam.id, 'connected');
      await api.put(`/api/cameras/${cam.id}`, { data: { search_indexing: true } });
      expect(await brokerCameras()).toContain(cam.slug);

      const off = await api.put(`/api/cameras/${cam.id}`, {
        data: { search_indexing: false },
      });
      expect(off.status(), await off.text()).toBe(200);

      // Symmetry is the half that matters for data protection: an operator who
      // stops collection has to be able to believe it stopped, not "will stop
      // within a minute".
      expect(await indexCameras(),
        'the index kept ingesting after indexing was switched off',
      ).not.toContain(cam.slug);
      expect(await brokerCameras(),
        'the broker kept decoding a camera with no consumer left',
      ).not.toContain(cam.slug);
    } finally {
      await api.dispose();
    }
  });

  test('selecting no domain at all is stored as off, everywhere', async () => {
    // "Indexed but contributing nothing" is a state the product refuses to
    // hold: `update_camera` removes such a camera rather than registering it.
    // Worth pinning across the boundary because the three services each decide
    // it separately, from rules that are easy to drift apart.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E AI No Domains' });
      await waitForHealth(api, cam.id, 'connected');
      await api.put(`/api/cameras/${cam.id}`, { data: { search_indexing: true } });
      expect(await indexCameras()).toContain(cam.slug);

      const none = await api.put(`/api/cameras/${cam.id}`, {
        data: { search_domains: [] },
      });
      expect(none.status(), await none.text()).toBe(200);

      expect(await indexCameras(),
        'a camera with no domains selected was left in the index looking active',
      ).not.toContain(cam.slug);
      expect(await brokerCameras()).not.toContain(cam.slug);
    } finally {
      await api.dispose();
    }
  });

  // ── The operator's own route to it ───────────────────────────────────────

  test('an operator switches indexing on from AI Config and the services follow',
    async ({ page }) => {
      // The same fix through the product's own screen. The dropdown saves on
      // change — there is no Save button for this control — so the assertion is
      // that the services hold the camera once the row reports it, not once a
      // form is submitted.
      const api = await service();
      let cam: any;
      try {
        cam = await registerCamera(api, 'one', { name: 'E2E AI By Hand' });
        await waitForHealth(api, cam.id, 'connected');
      } finally {
        await api.dispose();
      }

      await login(page, 'admin');
      await page.goto(`/#/cameras/config?cam=${cam.id}&tab=ai`);

      // The control says what the default is, which is the operator-visible
      // half of migration 038.
      const indexing = page.locator('select').filter({ hasText: 'exclude from Smart Search' });
      await expect(indexing).toBeVisible({ timeout: 60_000 });
      await expect(indexing).toHaveValue('false');

      await indexing.selectOption('true');
      // "Index what" only exists once indexing is on, and its boxes carry the
      // stored domains — the single click that works.
      await expect(page.getByText('Index what')).toBeVisible({ timeout: 30_000 });

      const after = await service();
      try {
        await expect
          .poll(async () => (await after.get(`/api/cameras/${cam.id}`)).json()
            .then((r: any) => r.search_indexing),
                { timeout: 30_000, intervals: [500],
                  message: 'the dropdown never saved' })
          .toBe(true);
        // Once the SAVE has landed the push has already happened, because the
        // endpoint awaits it before answering the request the SPA made.
        expect(await brokerCameras()).toContain(cam.slug);
        expect(await indexCameras()).toContain(cam.slug);
      } finally {
        await after.dispose();
      }
    });
});
