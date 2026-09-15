/**
 * J9 — deleting a camera: everything it created goes with it.
 *
 * WHY DELETION GETS ITS OWN JOURNEY. Registration is loud — if it half-works,
 * there is no picture and someone notices within seconds. Deletion is silent by
 * construction: the row disappears from the UI on the first response, and every
 * remaining trace is somewhere an operator cannot look. A relay path still
 * pulling from a camera nobody manages, an NVR worker still writing segments, a
 * low-resolution copy of exactly the footage a DPDP erasure claimed to destroy —
 * all of these leave the screen looking correct.
 *
 * THE CONTRACT BEING TESTED IS EXPLICIT IN THE PRODUCT. `delete_camera` and
 * `_teardown_recording_tracks` both state it: BOTH tracks are torn down and
 * purged unconditionally, without asking whether the sub is switched on right
 * now, because deletion is the one path the reconcile loops cannot heal — once
 * the row is gone, nothing remembers this camera ever had a sub. The comment
 * names the failure it was written against: gating the purge on the flag
 * "reported erasure as complete while a full low-res copy of exactly that
 * footage stayed on disk". So `<slug>_sub` is not an implementation detail here.
 * It is the invariant.
 *
 * WHERE THE SUB TRACK COMES FROM. `substream.judge` correctly refuses a sub for
 * a browser-safe main, and the synthetic cameras are H.264 on purpose so the
 * live-view journeys have something a browser can decode. The resolved sub_track
 * row is therefore seeded — see `seedResolvedSubTrack` for the full reasoning —
 * and everything above it is the product: the relay's reconcile loop creates the
 * path, `PUT /sub-track` starts its recording, and `DELETE /cameras/{id}` is
 * what these tests are actually about.
 */
import { expect, test } from '@playwright/test';
import { service } from '../support/api';
import { login } from '../support/auth';
import {
  LAWFUL_BASIS, nvrCameras, registerCamera, waitForHealth, waitForRecording,
} from '../support/cameras';
import { resetAppliance } from '../support/reset';
import {
  cameraRtspUrl, relayHasPath, relayPathNames, restoreCameras, seedResolvedSubTrack,
} from '../support/stack';

/** Relay reconcile and NVR teardown are both interval-driven; this is the
 *  budget for waiting them out, not a statement about how long they take. */
test.describe.configure({ timeout: 300_000 });

const SUB = (slug: string) => `${slug}_sub`;

/** Wait for the relay to hold — or no longer hold — a path. */
async function expectPath(name: string, present: boolean, why: string) {
  await expect
    .poll(() => relayHasPath(name),
          { timeout: 90_000, intervals: [2000], message: why })
    .toBe(present);
}

test.describe('J9 · camera deletion', () => {
  test.beforeEach(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test.afterAll(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  // ── What deletion has to remove ──────────────────────────────────────────

  test('onboarding creates the relay path that deletion has to remove', async () => {
    // THE PRECONDITION FOR THIS WHOLE JOURNEY. "The path is gone" proves
    // nothing unless the path was there — which is the same trap J8 avoids by
    // checking the foreign row really reached the index.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Delete Precondition' });
      await waitForHealth(api, cam.id, 'connected');
      await expectPath(cam.slug, true, 'the camera never got a relay path');
    } finally {
      await api.dispose();
    }
  });

  test('deleting a camera takes its relay path with it', async () => {
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Delete Relay' });
      await waitForHealth(api, cam.id, 'connected');
      await expectPath(cam.slug, true, 'the camera never got a relay path');

      expect((await api.delete(`/api/cameras/${cam.id}`)).status()).toBe(204);

      // A path left behind keeps an RTSP session open against a camera the
      // appliance no longer manages, and nothing on any screen would say so.
      await expectPath(cam.slug, false,
        'the relay kept pulling from a camera that was deleted');
    } finally {
      await api.dispose();
    }
  });

  test('deleting a camera removes its SUB track from the relay too', async () => {
    // THE TWO-TRACK INVARIANT, at the one place the reconcile loops cannot
    // heal it. After the row is gone, nothing in the appliance remembers this
    // camera ever had a sub — so a teardown that misses it leaves a path no
    // later pass will ever collect.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Delete Sub Relay' });
      await waitForHealth(api, cam.id, 'connected');
      await seedResolvedSubTrack(cam.slug, 'one');

      // The relay's own reconcile creates it: `relay_tracks_for` carries a
      // resolved sub on demand, so the live view has a fallback for a codec
      // the browser cannot decode. Nothing here touches MediaMTX directly.
      await expectPath(SUB(cam.slug), true,
        'the relay never carried the resolved sub track');

      expect((await api.delete(`/api/cameras/${cam.id}`)).status()).toBe(204);

      // CHECKED ONCE, IMMEDIATELY — NOT POLLED, and that is the whole test.
      //
      // `delete_camera` awaits `relay.remove_path()` for both tracks before it
      // answers 204, so by the time this client holds the response the paths
      // must already be gone. There is no race to wait out.
      //
      // A POLL HERE IS WORSE THAN USELESS. The health monitor runs an orphan
      // sweep every HEALTH_POLL_INTERVAL (30 s) that removes any relay path
      // belonging to no registry camera — and once the row is deleted, that is
      // exactly what `<slug>_sub` is. The sweep is a deliberate backstop, not a
      // bug: a path left pulling can hold an RTSP session against hardware that
      // allows only one or two, locking out the camera's re-added successor.
      // But it means a polling assertion passes whether or not deletion did its
      // job — which is precisely what happened here. Mutation-testing this file
      // by removing `relay.remove_path(sub_name)` from `delete_camera` left
      // this test GREEN, at 56 s instead of 27 s, because it sat waiting for
      // the sweep. Asserting at the instant of the 204 is what separates
      // "deletion tore both tracks down" from "something eventually tidied up".
      const after = await relayPathNames();
      expect(after,
        'the main relay path was still present when deletion returned 204',
      ).not.toContain(cam.slug);
      expect(after,
        'THE SUB TRACK\'S RELAY PATH WAS STILL PRESENT WHEN DELETION RETURNED '
        + '204 — deletion did not tear it down; the health monitor\'s orphan '
        + 'sweep will mask this within 30s, which is why this is checked now',
      ).not.toContain(SUB(cam.slug));
    } finally {
      await api.dispose();
    }
  });

  test('deleting a camera stops recording on both of its tracks', async () => {
    // The NVR side of the same invariant. A worker left running writes
    // segments for a camera with no row — footage that cannot be viewed,
    // cannot be groomed through the UI, and cannot be erased on request.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', {
        name: 'E2E Delete Recording', recording: true,
      });
      await waitForHealth(api, cam.id, 'connected');
      await seedResolvedSubTrack(cam.slug, 'one');

      // Switching the sub's recording on is an operator action, so it goes
      // through the operator's endpoint.
      const on = await api.put(`/api/cameras/${cam.id}/sub-track`, {
        data: { recording_enabled: true },
      });
      expect(on.status(), await on.text()).toBe(200);

      await waitForRecording(api, cam.slug);
      await expect
        .poll(async () => (await nvrCameras(api)).map((r: any) => r.name),
              { timeout: 120_000, intervals: [3000],
                message: 'the NVR never picked up the sub track' })
        .toContain(SUB(cam.slug));

      expect((await api.delete(`/api/cameras/${cam.id}?purge_recordings=true`))
        .status()).toBe(204);

      await expect
        .poll(async () => (await nvrCameras(api)).map((r: any) => r.name),
              { timeout: 120_000, intervals: [3000],
                message: 'a track was still recording after its camera was deleted' })
        .toEqual([]);
    } finally {
      await api.dispose();
    }
  });

  // ── What the operator sees ───────────────────────────────────────────────

  test('a deleted camera is gone from the API', async () => {
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Delete Gone' });
      await waitForHealth(api, cam.id, 'connected');

      expect((await api.delete(`/api/cameras/${cam.id}`)).status()).toBe(204);

      expect((await api.get(`/api/cameras/${cam.id}`)).status()).toBe(404);
      const rows: any[] = await (await api.get('/api/cameras')).json();
      expect(rows.map((c) => c.id)).not.toContain(cam.id);
    } finally {
      await api.dispose();
    }
  });

  test('an operator can delete a camera from the config page', async ({ page }) => {
    // The product's own destructive flow, including the confirmation the UI
    // puts in front of it — not a DELETE issued around the interface.
    const api = await service();
    let cam: any;
    try {
      cam = await registerCamera(api, 'one', { name: 'E2E Delete By Hand' });
      await waitForHealth(api, cam.id, 'connected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto(`/#/cameras/config?cam=${cam.id}`);

    // Identity is checked in the DIALOG, not by finding the name on the page.
    // The config page has a camera picker whose <option> elements carry the
    // same name, and a page-wide getByText finds that hidden option first —
    // reporting an invisible camera on a page that shows it perfectly clearly.
    // The dialog is also the better place for the check: it is what tells the
    // operator which identity is about to be destroyed.
    const deleteButton = page.getByRole('button', { name: 'Delete camera' });
    await expect(deleteButton).toBeVisible({ timeout: 60_000 });
    await deleteButton.click();
    // `.modal`, not getByRole('dialog'): the product's Modal renders a plain
    // div with no `role="dialog"` or `aria-modal`, so there is no dialog role
    // to select. Reported as an accessibility observation rather than worked
    // around silently — the test uses the class because that is what the
    // product actually renders today.
    const dialog = page.locator('.modal');
    // The dialog names the slug, so an operator can see which identity is
    // about to be destroyed. Confirming is a second, separate button.
    await expect(dialog).toContainText(cam.slug);
    await dialog.getByRole('button', { name: 'Delete camera' }).click();

    await expect(page.getByText('E2E Delete By Hand')).toHaveCount(0, {
      timeout: 60_000,
    });

    const after = await service();
    try {
      expect((await after.get(`/api/cameras/${cam.id}`)).status()).toBe(404);
      await expectPath(cam.slug, false,
        'the relay path survived a deletion made through the UI');
    } finally {
      await after.dispose();
    }
  });

  test('erasing footage is a deliberate, separately confirmed act', async ({ page }) => {
    // Purging is not a checkbox alone: the operator has to type DELETE. This
    // is the guard between "remove this camera" and "destroy the evidence it
    // recorded", and it is the kind of thing that quietly stops working.
    const api = await service();
    let cam: any;
    try {
      cam = await registerCamera(api, 'one', { name: 'E2E Delete Confirm' });
      await waitForHealth(api, cam.id, 'connected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto(`/#/cameras/config?cam=${cam.id}`);
    await page.getByRole('button', { name: 'Delete camera' }).click();
    const dialog = page.locator('.modal');

    await dialog.getByRole('checkbox').check();
    const confirm = dialog.getByRole('button', { name: 'Delete camera + footage' });
    await expect(confirm, 'footage could be erased without typing the confirmation')
      .toBeDisabled();

    await dialog.getByPlaceholder('DELETE').fill('DELETE');
    await expect(confirm).toBeEnabled();
  });

  // ── What deletion leaves behind, and what it must not ────────────────────

  test('deletion frees the camera\'s identity for re-use', async () => {
    // `rtsp_url` and `slug` are both uniquely indexed, so a row that was not
    // really removed shows up as a 409 on the next attempt to add the same
    // camera — the operator-visible symptom of an incomplete delete.
    const api = await service();
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Delete Reuse' });
      await waitForHealth(api, cam.id, 'connected');
      expect((await api.delete(`/api/cameras/${cam.id}`)).status()).toBe(204);

      const again = await api.post('/api/cameras', {
        data: {
          name: 'E2E Delete Reuse Again',
          rtsp_url: cameraRtspUrl('one'),
          lawful_basis: LAWFUL_BASIS,
          purpose: 'E2E journey — re-adding a deleted camera',
        },
      });
      expect(again.status(), await again.text()).toBe(201);
      // A NEW identity, which is what the delete dialog promises: "re-adding it
      // later creates a new identity (new slug/URL/timeline)".
      expect((await again.json()).id).not.toBe(cam.id);
    } finally {
      await api.dispose();
    }
  });

  test('the deletion is recorded, including what it claims to have erased',
    async () => {
      // The audit line is how an erasure request is answered. `purge_complete`
      // is the product's own statement that BOTH tracks were purged — recorded
      // rather than raised, precisely so a half-purge cannot be reported as a
      // purge.
      const api = await service();
      try {
        const cam = await registerCamera(api, 'one', {
          name: 'E2E Delete Audit', recording: true,
        });
        await waitForHealth(api, cam.id, 'connected');
        await waitForRecording(api, cam.slug);

        expect((await api.delete(`/api/cameras/${cam.id}?purge_recordings=true`))
          .status()).toBe(204);

        const audit = await api.get('/api/audit?action=camera.deleted&limit=50');
        expect(audit.status()).toBe(200);
        const entries: any[] = (await audit.json()).entries ?? [];
        const line = entries.find((e) => e.target === cam.slug);
        expect(line, `no audit line for deleting ${cam.slug}`).toBeTruthy();
        expect(line.detail?.purged_recordings).toBe(true);
        expect(line.detail?.purge_complete,
          'the appliance recorded an INCOMPLETE purge — footage may remain on '
          + 'disk for a camera whose row is gone').toBe(true);
      } finally {
        await api.dispose();
      }
    });

  test('deleting one camera leaves the other alone', async () => {
    // The blast radius. Teardown works on names derived from a slug, and
    // `<slug>_sub` is one prefix away from a sibling camera's paths.
    const api = await service();
    try {
      const keep = await registerCamera(api, 'two', { name: 'E2E Delete Bystander' });
      const drop = await registerCamera(api, 'one', { name: 'E2E Delete Target' });
      await waitForHealth(api, keep.id, 'connected');
      await waitForHealth(api, drop.id, 'connected');

      expect((await api.delete(`/api/cameras/${drop.id}`)).status()).toBe(204);

      await expectPath(drop.slug, false, 'the deleted camera kept its relay path');
      expect(await relayPathNames(),
        'deleting one camera took another camera\'s relay path with it')
        .toContain(keep.slug);
      expect((await api.get(`/api/cameras/${keep.id}`)).status()).toBe(200);
    } finally {
      await api.dispose();
    }
  });

  test('deleting a camera that does not exist is refused, not invented', async () => {
    const api = await service();
    try {
      const resp = await api.delete(
        '/api/cameras/00000000-0000-4000-8000-000000000000');
      expect(resp.status()).toBe(404);
    } finally {
      await api.dispose();
    }
  });
});
