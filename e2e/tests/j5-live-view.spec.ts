/**
 * J5 — live view: the right picture, under the right name.
 *
 * THE FAILURE THIS JOURNEY EXISTS FOR is not "no video". No video is obvious,
 * and somebody reports it within the hour. The dangerous one is video from the
 * WRONG camera under the right label — an operator watching the loading bay
 * while the tile says front gate, and nothing anywhere says otherwise. On a
 * surveillance product that is the worst class of defect there is, and it is
 * invisible to every test below this one.
 *
 * HOW IDENTITY IS PROVEN WITHOUT LOOKING AT PIXELS. The two synthetic cameras
 * are deliberately different shapes: cam-1 is 1280x720 and cam-2 is 1024x768.
 * A `<video>` element exposes `videoWidth`/`videoHeight` of the stream it has
 * actually decoded, so reading them off the tile is a deterministic equality
 * check on which camera's frames arrived there. No screenshot, no OCR, no
 * judgement call.
 *
 * The 4:3 camera is not decoration either: the video wall used to crop any
 * non-16:9 source (`objectFit: 'cover'`), invisible on a 16:9 camera. That is
 * fixed and guarded by the unit test in `frontend-react/.../LiveVideo.test.tsx`;
 * this journey proves the 4:3 shape arrives intact, so a future crop can be
 * attributed to the UI rather than the source.
 *
 * STREAM STARTUP IS A STATE, NOT A DURATION. `videoWidth > 0` means frames have
 * been decoded. Waiting for that is waiting for the thing itself; waiting three
 * seconds is guessing, and would be the suite's first flake.
 */
import { expect, test, type Page } from '@playwright/test';
import { service } from '../support/api';
import { login } from '../support/auth';
import { registerCamera, waitForHealth } from '../support/cameras';
import { CAMERAS } from '../support/env';
import { resetAppliance } from '../support/reset';
import { restoreCameras, startCamera, stopCamera } from '../support/stack';

/** What each synthetic camera decodes to. The whole identity argument rests
 *  on these differing. */
const SHAPE = {
  one: { width: 1280, height: 720 },
  two: { width: 1024, height: 768 },
} as const;

/**
 * The decoded dimensions of the <video> inside the tile that names `slug`.
 *
 * Scoped to the tile rather than to the page: with two cameras up there are two
 * <video> elements, and a test that grabbed "the first one" would pass no
 * matter which camera's frames were in it — exactly the bug being hunted.
 */
async function tileVideoShape(page: Page, slug: string) {
  return page.evaluate((wanted) => {
    const tiles = Array.from(document.querySelectorAll('.lv-tile'));
    const tile = tiles.find((t) => (t.textContent ?? '').includes(wanted));
    if (!tile) return { found: false, width: 0, height: 0, readyState: -1 };
    const v = tile.querySelector('video') as HTMLVideoElement | null;
    if (!v) return { found: true, width: 0, height: 0, readyState: -1 };
    return { found: true, width: v.videoWidth, height: v.videoHeight, readyState: v.readyState };
  }, slug);
}

/**
 * Is the "Signal lost" overlay showing in the tile that names `slug`?
 *
 * SCOPED, because LiveVideo renders the overlay for EVERY tile and toggles it
 * with `display`. A page-wide `getByText(/Signal lost/)` therefore matches the
 * hidden overlay of a camera that is perfectly fine, and `.first()` picks
 * whichever is first in DOM order — so with two cameras up the assertion waits
 * on the survivor's invisible overlay and times out while the product is doing
 * exactly the right thing.
 */
async function tileSignalLost(page: Page, slug: string): Promise<boolean> {
  return page.evaluate((wanted) => {
    const tiles = Array.from(document.querySelectorAll('.lv-tile'));
    const tile = tiles.find((t) => (t.textContent ?? '').includes(wanted));
    if (!tile) return false;
    // Checked on the ELEMENT, not on its parent. The overlay's display toggle
    // sits two levels above the text node, so inspecting `parentElement` reads
    // an always-visible wrapper and reports every tile as offline — including
    // the one that is happily playing.
    return Array.from(tile.querySelectorAll('div'))
      .filter((d) => /^\s*Signal lost/i.test(d.textContent ?? ''))
      .some((d) => {
        const el = d as HTMLElement;
        return typeof el.checkVisibility === 'function'
          ? el.checkVisibility()
          : el.offsetParent !== null;
      });
  }, slug);
}

/**
 * Wait until that tile has actually decoded a frame.
 *
 * `videoWidth > 0` alone is NOT enough: it becomes non-zero at HAVE_METADATA
 * (readyState 1), which means the stream's shape is known and no picture has
 * arrived yet. Waiting only on the width and then asserting readyState >= 2 is
 * therefore a race the test loses about half the time — as this one did.
 * HAVE_CURRENT_DATA (2) is the state that means "there is a frame to show".
 */
async function waitForTilePlaying(page: Page, slug: string, timeout = 90_000) {
  await expect
    .poll(async () => {
      const s = await tileVideoShape(page, slug);
      return s.width > 0 && s.readyState >= 2;
    }, { timeout, intervals: [500], message: `no frame decoded in the tile for ${slug}` })
    .toBe(true);
}

test.describe('J5 · live view', () => {
  test.beforeEach(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test.afterAll(async () => {
    await restoreCameras();
    await resetAppliance();
  });

  test('a camera plays in the live view', async ({ page }) => {
    const api = await service();
    let slug = '';
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Live One' });
      slug = cam.slug;
      await waitForHealth(api, cam.id, 'connected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/live');
    await expect(page.getByText('E2E Live One').first()).toBeVisible({ timeout: 60_000 });

    await waitForTilePlaying(page, slug);
    const shape = await tileVideoShape(page, slug);
    expect(shape.readyState).toBeGreaterThanOrEqual(2); // HAVE_CURRENT_DATA
  });

  test('the tile shows the stream from the camera it names', async ({ page }) => {
    // THE ONE THAT MATTERS. cam-1 decodes 1280x720; if cam-2's frames arrived
    // in this tile they would measure 1024x768.
    const api = await service();
    let slug = '';
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Identity 16x9' });
      slug = cam.slug;
      await waitForHealth(api, cam.id, 'connected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/live');
    await waitForTilePlaying(page, slug);

    const shape = await tileVideoShape(page, slug);
    expect(shape.width).toBe(SHAPE.one.width);
    expect(shape.height).toBe(SHAPE.one.height);
  });

  test('two cameras play side by side, each under its own name', async ({ page }) => {
    // The crossover case. With one camera up, a wiring bug that always serves
    // "the first stream" is invisible; it only shows with two.
    const api = await service();
    let one = '', two = '';
    try {
      const a = await registerCamera(api, 'one', { name: 'E2E Wall Wide' });
      const b = await registerCamera(api, 'two', { name: 'E2E Wall Tall' });
      one = a.slug; two = b.slug;
      await waitForHealth(api, a.id, 'connected');
      await waitForHealth(api, b.id, 'connected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/live');
    await expect(page.getByText('E2E Wall Wide').first()).toBeVisible({ timeout: 60_000 });
    await expect(page.getByText('E2E Wall Tall').first()).toBeVisible();

    await waitForTilePlaying(page, one);
    await waitForTilePlaying(page, two);

    const wide = await tileVideoShape(page, one);
    const tall = await tileVideoShape(page, two);

    expect(wide.width, 'the 16:9 camera did not decode 1280x720').toBe(SHAPE.one.width);
    expect(wide.height).toBe(SHAPE.one.height);
    expect(tall.width, 'the 4:3 camera did not decode 1024x768').toBe(SHAPE.two.width);
    expect(tall.height).toBe(SHAPE.two.height);

    // Said explicitly, because it is the actual claim: the two tiles are not
    // showing the same stream.
    expect(wide.width).not.toBe(tall.width);
  });

  test('the aspect ratio the camera really has survives to the browser', async ({ page }) => {
    // The 4:3 camera exists for the wall's (now fixed) cell-crop defect.
    // Establishing that its shape arrives intact in a normal tile is what lets
    // any future crop be attributed to the UI rather than to the source.
    const api = await service();
    let slug = '';
    try {
      const cam = await registerCamera(api, 'two', { name: 'E2E Aspect 4x3' });
      slug = cam.slug;
      await waitForHealth(api, cam.id, 'connected');
    } finally {
      await api.dispose();
    }

    await login(page, 'admin');
    await page.goto('/#/live');
    await waitForTilePlaying(page, slug);

    const shape = await tileVideoShape(page, slug);
    expect(shape.width / shape.height).toBeCloseTo(4 / 3, 2);
  });

  test('the HLS playlist for a camera describes that camera', async () => {
    // The layer under the tile. If the playlist itself names the wrong
    // resolution, the browser was told the wrong thing before it decoded
    // anything — which separates a relay-side mix-up from a UI-side one.
    const api = await service();
    try {
      const a = await registerCamera(api, 'one', { name: 'E2E Playlist Wide' });
      const b = await registerCamera(api, 'two', { name: 'E2E Playlist Tall' });
      await waitForHealth(api, a.id, 'connected');
      await waitForHealth(api, b.id, 'connected');

      for (const [cam, shape] of [[a, SHAPE.one], [b, SHAPE.two]] as const) {
        await expect
          .poll(async () => {
            const resp = await api.get(`/hls/${cam.slug}/index.m3u8`);
            return resp.ok() ? resp.text() : '';
          }, { timeout: 90_000, intervals: [2000] })
          .toContain(`RESOLUTION=${shape.width}x${shape.height}`);
      }
    } finally {
      await api.dispose();
    }
  });

  // ── When the picture goes ────────────────────────────────────────────────

  test('a camera that stops streaming says so in its tile', async ({ page }) => {
    // NOT a frozen last frame. The overlay is the product telling an operator
    // that what they are looking at is not live — the single most important
    // thing a surveillance UI can be honest about.
    const api = await service();
    let slug = '';
    try {
      const cam = await registerCamera(api, 'one', { name: 'E2E Signal Lost' });
      slug = cam.slug;
      await waitForHealth(api, cam.id, 'connected');

      await login(page, 'admin');
      await page.goto('/#/live');
      await waitForTilePlaying(page, slug);

      await stopCamera(CAMERAS.one.container);

      await expect
        .poll(() => tileSignalLost(page, slug),
              { timeout: 90_000, intervals: [1000],
                message: 'the tile never said the signal was lost' })
        .toBe(true);
    } finally {
      await startCamera(CAMERAS.one.container);
      await api.dispose();
    }
  });

  // Two cameras to register, two to bring up and one to take down: more setup
  // than the default budget, and the wait is on real relay behaviour.
  test('one camera going down leaves the other playing', async ({ page }) => {
    test.setTimeout(200_000);
    // A live wall where one dead camera blanks the rest is worse than the one
    // dead camera.
    const api = await service();
    let one = '', two = '';
    try {
      const a = await registerCamera(api, 'one', { name: 'E2E Down One' });
      const b = await registerCamera(api, 'two', { name: 'E2E Stays Up' });
      one = a.slug; two = b.slug;
      await waitForHealth(api, a.id, 'connected');
      await waitForHealth(api, b.id, 'connected');

      await login(page, 'admin');
      await page.goto('/#/live');
      await waitForTilePlaying(page, one);
      await waitForTilePlaying(page, two);

      await stopCamera(CAMERAS.one.container);
      await expect
        .poll(() => tileSignalLost(page, one),
              { timeout: 90_000, intervals: [1000],
                message: 'the downed camera never reported a lost signal' })
        .toBe(true);

      // The survivor is still decoding, still its own shape, and NOT showing
      // the overlay — which is the half that makes this test meaningful.
      const survivor = await tileVideoShape(page, two);
      expect(survivor.width).toBe(SHAPE.two.width);
      expect(await tileSignalLost(page, two)).toBe(false);
    } finally {
      await restoreCameras();
      await api.dispose();
    }
  });

  test('the live view shows nothing rather than something wrong when empty', async ({ page }) => {
    // No cameras registered. The honest answer is an empty state, not a tile.
    await login(page, 'admin');
    await page.goto('/#/live');
    await expect(page.locator('.lv-tile')).toHaveCount(0, { timeout: 30_000 });
  });
});
