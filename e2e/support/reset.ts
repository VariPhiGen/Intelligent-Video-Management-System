/**
 * reset.ts — putting the appliance back, so journeys are independent.
 *
 * THE STANDARD REACHED FOR HERE is not "wipe everything between tests" — that
 * would mean re-migrating a database and re-importing a realm for every test,
 * minutes each. It is: after a journey runs, the next one finds the appliance
 * as if the first had not.
 *
 * That means removing what a journey CREATES (cameras, and with them their
 * relay paths and recordings) while leaving what the stack IS (the realm, the
 * schema, the services). Deletion goes through the product's own endpoint
 * rather than a SQL truncate, for two reasons: a truncate would leave the relay
 * holding paths for cameras that no longer exist — poisoning the next test with
 * state no product action could produce — and the delete path is itself the
 * teardown the two-track invariant is about.
 *
 * WHAT IS DELIBERATELY NOT RESET. The synthetic cameras keep running: they are
 * hardware, and hardware does not restart between tests. `restoreCameras()`
 * exists for the journey that stops one on purpose, and is called by that
 * journey rather than globally, so a failure to restore is attributed to the
 * test that caused it.
 */
import type { APIRequestContext } from '@playwright/test';
import { service } from './api';
import { clearScanJob, relayPathNames } from './stack';

/**
 * Remove every registered camera, through the product's own delete.
 *
 * Returns how many were removed, so a caller can tell "nothing to do" from
 * "cleanup ran" when a failure needs explaining.
 */
export async function deleteAllCameras(ctx?: APIRequestContext): Promise<number> {
  const api = ctx ?? (await service());
  const owned = !ctx;
  try {
    const resp = await api.get('/api/cameras');
    if (!resp.ok()) throw new Error(`GET /api/cameras -> ${resp.status()}`);
    const cameras: any[] = await resp.json();
    for (const cam of cameras) {
      // purge_recordings so the NVR does not accumulate footage across a run;
      // a later journey asserting on recording coverage must not inherit it.
      const del = await api.delete(
        `/api/cameras/${cam.id}?purge_recordings=true`,
      );
      if (!del.ok() && del.status() !== 404) {
        throw new Error(`DELETE camera ${cam.slug} -> ${del.status()} ${await del.text()}`);
      }
    }
    return cameras.length;
  } finally {
    if (owned) await api.dispose();
  }
}

/**
 * Remove the staged rows a scan leaves behind.
 *
 * A discovery scan writes cameras at stage='discovered'; they are not
 * registered cameras and `DELETE /api/cameras/{id}` is not their endpoint. Left
 * alone they make the next scan's device list non-empty before it starts, which
 * is exactly the kind of inherited state that makes a journey pass for the
 * wrong reason.
 */
export async function deleteDiscoveredDevices(ctx?: APIRequestContext): Promise<number> {
  const api = ctx ?? (await service());
  const owned = !ctx;
  try {
    const resp = await api.get('/api/discovery/devices');
    if (!resp.ok()) return 0;
    const body = await resp.json();
    const devices: any[] = body.devices ?? body.items ?? body ?? [];
    let removed = 0;
    for (const d of devices) {
      const id = d.id ?? d.device_id;
      if (!id) continue;
      const del = await api.delete(`/api/discovery/devices/${id}`);
      if (del.ok()) removed += 1;
    }
    return removed;
  } finally {
    if (owned) await api.dispose();
  }
}

/** Everything a journey may have created. */
export async function resetAppliance(): Promise<void> {
  const api = await service();
  try {
    await deleteAllCameras(api);
    await deleteDiscoveredDevices(api);
    // The wizard's first step renders from the scan JOB, not the device list.
    await clearScanJob();
  } finally {
    await api.dispose();
  }
}

/**
 * Assert the relay is empty, which is the observable proof that cleanup
 * actually happened rather than merely being attempted.
 *
 * Kept separate from `resetAppliance` so a test can choose to wait for it: path
 * removal is a call to MediaMTX that follows the database delete, so there is a
 * brief window where the row is gone and the path is not.
 */
export async function relayIsEmpty(): Promise<boolean> {
  return (await relayPathNames()).length === 0;
}
