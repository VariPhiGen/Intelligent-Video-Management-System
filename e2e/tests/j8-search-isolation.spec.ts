/**
 * J8 — Smart Search isolation: another site's rows never reach this screen.
 *
 * THE SITUATION THIS GUARDS. The CLIP index is shared with the analytics
 * appliance and is NOT scoped to this VMS. It holds crops from cameras
 * configured by hand on that appliance, rows scanned out of object storage with
 * no camera identity at all, and crops from cameras since removed from the
 * registry. `services/search_scope.py` is the only thing standing between those
 * rows and an operator's search results.
 *
 * WHY THIS CANNOT BE PROVEN LOWER DOWN. The backend suite covers the five
 * scoping rules against a stubbed store — it can show the SQL is built with a
 * filter. It cannot show that a row which really exists in a really shared
 * index does not really come back through the real API. The difference matters
 * because the failure mode is not an exception: it is somebody else's
 * surveillance footage rendered under this deployment's branding.
 *
 * HOW THE FOREIGN ROW IS CREATED, and why it is legitimate. Seeding goes
 * through SmartSearch's own `POST /observations` — the real ingest path the
 * analytics service uses — reached from inside the compose network. That is
 * deliberately BELOW the boundary being tested: no product endpoint offers a
 * way to write a row for a camera this VMS does not own, and none should.
 * Reads then go through `/api/search/*`, exactly as the browser does. Seeding
 * under the boundary and reading over it is the whole design.
 *
 * The test also asserts the foreign row IS in the index, by asking the index
 * directly. Without that, "the product did not show it" could simply mean the
 * row was never written, and the journey would pass while proving nothing.
 *
 * WHY EVERY CAMERA HERE RECORDS. `search_scope` keeps a hit only if it traces
 * all the way to retained footage, and rule 2 is "the recorder knows that
 * camera" — the api narrows to slugs present in the NVR's inventory. A
 * registered but non-recording camera is therefore not searchable at all, which
 * is the product being right: a hit an operator cannot open in Playback is a
 * dead end. The first version of this file registered cameras without
 * recording, so the api answered with EMPTY_DETECTIONS and the isolation
 * assertions passed against an empty feed — proving nothing at all.
 */
import { expect, test } from '@playwright/test';
import { service } from '../support/api';
import { login } from '../support/auth';
import { registerCamera, waitForHealth, waitForRecording } from '../support/cameras';
import { resetAppliance } from '../support/reset';
import {
  indexDetections, indexStats, registerIndexCamera, seedObservation,
} from '../support/smartsearch';
import { restoreCameras } from '../support/stack';

/** A camera name this VMS will never have in its registry. */
const FOREIGN_CAMERA = 'other-appliance-cam';

test.describe.configure({ timeout: 240_000 });

test.describe('J8 · Smart Search isolation', () => {
  test.beforeEach(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test.afterAll(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  /**
   * One camera this VMS owns and one it does not, both with a row in the index.
   *
   * Returns the owned camera plus the epoch-ms the rows were written at, so a
   * caller can bound its queries to this test's own data.
   */
  async function seedBothSides(api: any, name: string) {
    const owned = await registerCamera(api, 'one', { name, recording: true });
    await waitForHealth(api, owned.id, 'connected');
    // The NVR has to know this camera before the api will call it searchable —
    // see the note at the top of this file.
    await waitForRecording(api, owned.slug);

    // The index has to know both cameras before it will ingest for them.
    await registerIndexCamera(owned.slug);
    await registerIndexCamera(FOREIGN_CAMERA);

    const ts = Date.now() / 1000;
    const mine = await seedObservation({ camera: owned.slug, ts, colour: [30, 92, 123] });
    const theirs = await seedObservation({ camera: FOREIGN_CAMERA, ts, colour: [123, 58, 30] });

    expect(mine.accepted, `seeding the owned row failed: ${JSON.stringify(mine)}`).toBe(true);
    expect(theirs.accepted, `seeding the foreign row failed: ${JSON.stringify(theirs)}`).toBe(true);

    return { owned, sinceMs: Math.floor((ts - 60) * 1000) };
  }

  test('both rows really are in the shared index', async () => {
    // THE PRECONDITION FOR EVERY OTHER TEST HERE. If the foreign row is not in
    // the index, "the product did not show it" is not evidence of anything.
    const api = await service();
    try {
      const { owned, sinceMs } = await seedBothSides(api, 'E2E Scope Precondition');

      const feed = await indexDetections(sinceMs, 500);
      const cameras = new Set((feed.recent ?? []).map((r: any) => r.camera_id));
      expect(cameras, 'the owned row never reached the index').toContain(owned.slug);
      expect(cameras, 'the foreign row never reached the index').toContain(FOREIGN_CAMERA);
    } finally {
      await api.dispose();
    }
  });

  test('the detections feed shows this VMS\'s camera and not the other one', async () => {
    // The dashboard an operator actually looks at. Unscoped, it reported
    // another deployment's activity as this one's.
    const api = await service();
    try {
      const { owned, sinceMs } = await seedBothSides(api, 'E2E Scope Feed');

      const resp = await api.get(`/api/search/detections?since_ms=${sinceMs}&limit=500`);
      expect(resp.status(), await resp.text()).toBe(200);
      const body = await resp.json();

      const cameras = (body.recent ?? []).map((r: any) => r.camera_id);
      expect(cameras, 'the owned camera is missing from the feed').toContain(owned.slug);
      expect(cameras,
        'ANOTHER DEPLOYMENT\'S CAMERA APPEARED IN THIS VMS\'S DETECTIONS FEED',
      ).not.toContain(FOREIGN_CAMERA);
    } finally {
      await api.dispose();
    }
  });

  test('the feed the product returns is explicitly marked scoped', async () => {
    // The api REFUSES an unscoped answer rather than passing it through — a 502
    // saying the index "needs the update that filters detections by camera".
    // The flag is what makes that refusal possible, so it has to survive: an
    // index that stopped setting it must break the page loudly, not quietly
    // start showing another deployment's activity.
    //
    // Asserted alongside a non-empty feed on purpose. `EMPTY_DETECTIONS` also
    // carries `scoped: true`, so this assertion passes trivially whenever the
    // camera is not searchable — which is exactly how the first version of this
    // file passed while testing nothing.
    const api = await service();
    try {
      const { owned, sinceMs } = await seedBothSides(api, 'E2E Scope Flag');
      const body = await (await api.get(
        `/api/search/detections?since_ms=${sinceMs}&limit=200`)).json();
      expect(body.scoped).toBe(true);
      expect((body.recent ?? []).map((r: any) => r.camera_id),
        'the scoped flag was asserted against an empty feed').toContain(owned.slug);
    } finally {
      await api.dispose();
    }
  });

  test('a person search never returns a foreign camera\'s hit', async () => {
    // The search itself, through the product's own endpoint with a real
    // Keycloak-authorised principal behind it.
    const api = await service();
    try {
      const { owned } = await seedBothSides(api, 'E2E Scope Search');

      const resp = await api.post('/api/search/people', {
        // 96 is the endpoint's documented ceiling; 100 is a 422.
        data: { query: 'a person', top_k: 96, score_threshold: 0 },
      });
      expect(resp.status(), await resp.text()).toBe(200);
      const body = await resp.json();

      // Every result names its camera through `camera`, the registry reference
      // the api attaches so the SPA can deep-link into Playback.
      const cameras = (body.results ?? []).map(
        (r: any) => r.camera?.slug ?? r.camera_id ?? r.sensor_id);
      expect(cameras,
        'a foreign camera\'s crop was returned by this VMS\'s search',
      ).not.toContain(FOREIGN_CAMERA);
      // Stronger than naming the one foreign camera: NOTHING outside this
      // registry may come back, whatever it is called. The shared index also
      // holds bare sensor UUIDs and rows with no camera identity at all, and a
      // filter that let those through would pass a name-based check.
      for (const c of cameras) {
        expect(c, `search returned a camera this VMS does not own: ${c}`)
          .toBe(owned.slug);
      }
    } finally {
      await api.dispose();
    }
  });

  test('searchable cameras are only this VMS\'s cameras', async () => {
    // What the search page offers as a filter. A foreign camera listed here
    // would invite an operator to search a camera the appliance cannot open
    // footage for.
    const api = await service();
    try {
      const { owned } = await seedBothSides(api, 'E2E Scope Cameras');

      const resp = await api.get('/api/search/cameras');
      expect(resp.status()).toBe(200);
      const body = await resp.json();
      const names: string[] = (body.cameras ?? []).map((c: any) => c.slug);

      expect(names, 'the VMS\'s own recorded camera was not offered as searchable')
        .toContain(owned.slug);
      expect(names, 'a foreign camera was offered as searchable')
        .not.toContain(FOREIGN_CAMERA);
      // The filter offers registry cameras and nothing else, so it cannot
      // invite a search that has no footage behind it.
      expect(names).toEqual([owned.slug]);
    } finally {
      await api.dispose();
    }
  });

  test('search stats count this VMS\'s rows and not the whole index', async () => {
    // A total is a claim about the estate, and this is the one assertion in the
    // file that can only be made with a real shared index behind it: the
    // product's figure is compared against the index's OWN unscoped figure for
    // the same moment. The index knows about the foreign row; the product's
    // count must not.
    //
    // The product's docstring is explicit about what this guards — the shared
    // index's total "was overwhelmingly other deployments' rows", so "a page
    // could advertise thousands of entries and then answer every query with
    // nothing".
    const api = await service();
    try {
      await seedBothSides(api, 'E2E Scope Stats');

      const resp = await api.get('/api/search/stats');
      expect(resp.status()).toBe(200);
      const body = await resp.json();
      expect(body.reachable, 'the search index was not reachable').toBe(true);

      // `people`, not `person`: the api's domain key differs from the index's
      // own route (`/person/stats`), and asserting the wrong one reads as a
      // missing count rather than a naming difference.
      const scopedCount = body.domains?.people?.vectors_count;
      expect(typeof scopedCount, `no people count in ${JSON.stringify(body.domains)}`)
        .toBe('number');
      // Always true from a correct index; asserted because the flag is what the
      // product would have to trust if it ever stopped counting for itself.
      expect(body.domains.people.scoped).toBe(true);
      // One camera in scope: this VMS's own. The foreign camera has no registry
      // row and can never be counted here.
      expect(body.cameras).toBe(1);

      const wholeIndex = await indexStats('person');
      expect(wholeIndex.vectors_count,
        'the index does not hold more than this VMS owns, so this assertion '
        + 'could not distinguish a scoped count from an unscoped one')
        .toBeGreaterThan(scopedCount);
    } finally {
      await api.dispose();
    }
  });

  test('the Smart Search page offers this VMS\'s camera and not the other one',
    async ({ page }) => {
      // The end of the line: what is actually on screen for an operator. The
      // camera filter is where a foreign camera would surface first — it is
      // rendered straight from `/api/search/cameras`, and offering one would
      // invite a search the appliance has no footage for.
      const api = await service();
      let name = 'E2E Scope UI';
      try {
        await seedBothSides(api, name);
      } finally {
        await api.dispose();
      }

      await login(page, 'admin');
      await page.goto('/#/smartsearch');

      // The filter names cameras by their display name, so finding it proves
      // the page really loaded the scoped list — without which the assertion
      // below would pass on an empty page.
      // The field wraps its <select> in a <label> whose text is the caption,
      // so scoping by that caption gives exactly one control.
      const filter = page.locator('label.ss-field')
        .filter({ hasText: 'Camera' }).locator('select');
      await expect(filter).toContainText(name, { timeout: 60_000 });
      await expect(filter, 'a foreign camera was offered in the search filter')
        .not.toContainText(FOREIGN_CAMERA);

      // And nowhere else on the page either — results, empty states, tooltips.
      await expect(page.locator('body')).not.toContainText(FOREIGN_CAMERA);
    });

  test('deleting the owned camera removes its hits from search', async () => {
    // A camera removed from the registry stops being searchable, even though
    // its rows are still in the shared index. That is rule one of the scope:
    // a hit must resolve to a camera THIS VMS currently records.
    const api = await service();
    try {
      const { owned, sinceMs } = await seedBothSides(api, 'E2E Scope After Delete');

      const before = await (await api.get(
        `/api/search/detections?since_ms=${sinceMs}&limit=500`)).json();
      const beforeCams = (before.recent ?? []).map((r: any) => r.camera_id);
      expect(beforeCams).toContain(owned.slug);

      const del = await api.delete(`/api/cameras/${owned.id}?purge_recordings=true`);
      expect(del.status()).toBe(204);

      await expect
        .poll(async () => {
          const body = await (await api.get(
            `/api/search/detections?since_ms=${sinceMs}&limit=500`)).json();
          return (body.recent ?? []).map((r: any) => r.camera_id).includes(owned.slug);
        }, { timeout: 60_000, intervals: [2000],
             message: 'a deleted camera\'s hits were still searchable' })
        .toBe(false);
    } finally {
      await api.dispose();
    }
  });
});
