/**
 * cameras.ts — registering the synthetic cameras, and waiting on what the
 * product says about them.
 *
 * EVERY WAIT HERE IS ON A STATE THE PRODUCT REPORTS, never on a duration.
 * That is not merely a style rule: the times involved are genuinely variable —
 * MediaMTX has to open an RTSP session, the health monitor polls on its own
 * schedule, and the NVR indexes a segment only once it closes. A sleep long
 * enough to be safe would make the suite unusable, and a shorter one would be
 * the first flake.
 *
 * The bounded timeouts below are generous for that reason. They exist to turn
 * a hang into a readable failure, not to express how long anything should take.
 */
import { expect, type APIRequestContext } from '@playwright/test';
import { CAMERAS } from './env';
import { cameraRtspUrl } from './stack';

/** The lawful basis every E2E camera registers under. Any member of
 *  models.LAWFUL_BASES works; the DPDP gate only cares that it is one of them. */
export const LAWFUL_BASIS = 'Public safety / State function';

export interface RegisteredCamera {
  id: string;
  slug: string;
  name: string;
}

/**
 * Register one of the synthetic cameras through the product's own endpoint.
 *
 * `recording` defaults to FALSE. Recording is the expensive part — it makes the
 * NVR open a second session and write to disk — so a journey that is not about
 * recording should not pay for it, and J6 asks for it explicitly.
 */
export async function registerCamera(
  api: APIRequestContext,
  which: keyof typeof CAMERAS,
  opts: { name?: string; recording?: boolean } = {},
): Promise<RegisteredCamera> {
  const name = opts.name ?? `E2E ${which}`;
  const resp = await api.post('/api/cameras', {
    data: {
      name,
      rtsp_url: cameraRtspUrl(which),
      lawful_basis: LAWFUL_BASIS,
      purpose: `E2E journey — ${name}`,
      recording: opts.recording ?? false,
    },
  });
  if (resp.status() !== 201) {
    throw new Error(`registering ${name} failed: ${resp.status()} ${await resp.text()}`);
  }
  const body = await resp.json();
  return { id: body.id, slug: body.slug, name };
}

/**
 * One camera's row, read from the LIST rather than from `/cameras/{id}`.
 *
 * NOT AN ARBITRARY CHOICE: the list is what the product itself consumes.
 * `useCameras()` feeds the config page and HealthTab renders
 * `camera.ready_since` from it, so reading the same surface the UI reads is
 * what makes these assertions about the product rather than about an endpoint
 * nothing uses.
 *
 * This comment used to say something stronger — that `ready_since` was ALWAYS
 * null on `/cameras/{id}`, because only `list_cameras` enriched it. That was
 * true when this helper was written and was recorded as a product defect
 * (BUG-006); it was fixed on 2026-09-11, and `get_camera` now enriches the same
 * field from `relay.get_path_status`. Both endpoints agree, guarded by
 * `services/camera-mgmt/tests/test_ready_since_contract.py`. The helper keeps
 * reading the list for the reason above, not because the detail endpoint lies.
 *
 * Returns `null` when the camera is not in the list, so a poll can treat "not
 * visible yet" as an intermediate state instead of an error.
 */
export async function cameraRow(api: APIRequestContext, id: string): Promise<any | null> {
  const resp = await api.get('/api/cameras');
  if (!resp.ok()) return null;
  const rows: any[] = await resp.json();
  return rows.find((c) => c.id === id) ?? null;
}

/** The health detail the camera page shows: connected, tracks, reconnects. */
export async function cameraHealth(api: APIRequestContext, id: string): Promise<any> {
  const resp = await api.get(`/api/cameras/${id}/health`);
  if (!resp.ok()) throw new Error(`GET camera health ${id} -> ${resp.status()}`);
  return resp.json();
}

/**
 * Wait until the product reports a particular health status.
 *
 * `connected` means MediaMTX has an open session pulling from the camera —
 * which is the difference between "a row exists" and "there is a picture".
 */
export async function waitForHealth(
  api: APIRequestContext,
  id: string,
  status: 'connected' | 'disconnected',
  timeout = 180_000,
): Promise<void> {
  await expect
    .poll(async () => (await cameraRow(api, id))?.health_status ?? 'absent',
          { timeout, intervals: [2000], message: `camera ${id} never became ${status}` })
    .toBe(status);
}

/**
 * The NVR's camera rows, unwrapped.
 *
 * `GET /api/nvr/cameras` answers `{"cameras": [...]}`, NOT a bare array. The
 * first version of this helper assumed an array because an exploratory probe
 * had already unwrapped it before printing — so every J6 test died with
 * "rows.find is not a function" before touching the product. Unwrapping in one
 * place is what stops the next caller repeating it.
 */
export async function nvrCameras(api: APIRequestContext): Promise<any[]> {
  const resp = await api.get('/api/nvr/cameras');
  if (!resp.ok()) return [];
  const body = await resp.json();
  return Array.isArray(body) ? body : (body.cameras ?? body.items ?? []);
}

/**
 * Wait until the NVR has indexed footage for a camera.
 *
 * Indexing lags recording on purpose: a segment is only indexed once it closes,
 * so the newest footage is legitimately absent for a while. That lag is why
 * this is a poll and not an assertion.
 */
export async function waitForRecording(
  api: APIRequestContext,
  slug: string,
  timeout = 240_000,
): Promise<any> {
  await expect
    .poll(async () => {
      const row = (await nvrCameras(api)).find((r) => r.name === slug);
      return row?.segments_indexed ?? 0;
    }, { timeout, intervals: [3000], message: `the NVR never indexed a segment for ${slug}` })
    .toBeGreaterThan(0);

  return (await nvrCameras(api)).find((r) => r.name === slug);
}

/** The recorded window the NVR holds for a camera. */
export async function recordedRange(api: APIRequestContext, slug: string): Promise<any> {
  const resp = await api.get(`/api/nvr/cameras/${slug}/range`);
  if (!resp.ok()) throw new Error(`GET range ${slug} -> ${resp.status()}`);
  return resp.json();
}

/**
 * Wait until the NVR holds at least `seconds` of continuous footage.
 *
 * SEPARATE FROM `waitForRecording`, and the difference is the whole reason this
 * exists. `waitForRecording` returns the moment the FIRST segment is indexed,
 * which is the right gate for "is this camera recording at all" but leaves the
 * recorded span at whatever one segment happens to be. A test that then samples
 * two moments far enough apart to be distinguishable has no footage to sample,
 * and fails on the appliance's timing rather than on the product.
 *
 * Polling the range until it is long enough is state-based: it waits for the
 * condition the test actually needs instead of sleeping for a guess.
 */
export async function waitForRecordedSpan(
  api: APIRequestContext,
  slug: string,
  seconds: number,
  timeout = 240_000,
): Promise<any> {
  await expect
    .poll(async () => {
      const r = await api.get(`/api/nvr/cameras/${slug}/range`);
      if (!r.ok()) return 0;
      const body = await r.json();
      if (!body?.earliest || !body?.latest) return 0;
      return (Date.parse(body.latest) - Date.parse(body.earliest)) / 1000;
    }, {
      timeout,
      intervals: [3000],
      message: `the NVR never accumulated ${seconds}s of footage for ${slug}`,
    })
    .toBeGreaterThanOrEqual(seconds);

  return recordedRange(api, slug);
}

/**
 * ISO-8601 with a `Z`, never a `+00:00` offset.
 *
 * The NVR's own responses use `+00:00`, and feeding that straight back into a
 * query string fails: `+` decodes to a space, the timestamp no longer parses,
 * and the request 422s with "Invalid timestamp format. Use ISO 8601." — which
 * reads as the product rejecting its own output. Encoding would work too; `Z`
 * is simply the form that cannot be got wrong.
 */
export function isoZ(value: string | Date): string {
  const s = value instanceof Date ? value.toISOString() : value;
  return s.replace('+00:00', 'Z');
}

/**
 * Wait until the product reports when a camera's stream became ready.
 *
 * SEPARATE FROM `waitForHealth`, because the two do not land together and the
 * product never said they would. `health_status` comes from the health
 * monitor's own poll; `ready_since` is enriched onto the list from MediaMTX's
 * readyTime when the list is requested. A camera is routinely `connected` for
 * a moment before an enrichment pass has supplied its ready time, so asserting
 * both from one wait fails intermittently — which is exactly how this helper
 * came to exist.
 */
export async function waitForReadySince(
  api: APIRequestContext,
  id: string,
  timeout = 120_000,
): Promise<string> {
  await expect
    .poll(async () => (await cameraRow(api, id))?.ready_since ?? null,
          { timeout, intervals: [2000], message: `camera ${id} never reported a ready time` })
    .not.toBeNull();
  return (await cameraRow(api, id)).ready_since;
}
