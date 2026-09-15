/**
 * stack.ts — the few things a journey needs that are below the product's API.
 *
 * EVERYTHING HERE GOES THROUGH `docker exec`, and that is a deliberate limit
 * rather than a convenience. MediaMTX's control port is unpublished on purpose,
 * so reading the relay's path table means asking from inside the network. Doing
 * it this way keeps the host free of stack ports, which is what lets an E2E run
 * sit beside a developer's own stack.
 *
 * TWO KINDS OF THING LIVE HERE AND THEY ARE NOT THE SAME.
 *
 *   Observations   the relay path table. Used for assertions the UI genuinely
 *                  cannot make — "deleting a camera took its sub track away
 *                  too" is invisible on screen and is exactly the drift the
 *                  two-track invariant exists to prevent.
 *
 *   Interventions  stopping a camera container. This is not test scaffolding
 *                  reaching into the product; it is the physical event the
 *                  product is supposed to notice. A camera losing power has to
 *                  be simulated from outside the VMS, because from inside there
 *                  is nothing to call.
 */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { CAMERAS, ONVIF } from './env';

const run = promisify(execFile);

async function docker(args: string[], timeoutMs = 20_000): Promise<string> {
  const { stdout } = await run('docker', args, { timeout: timeoutMs });
  return stdout;
}

/** Run a snippet of Python inside a container that sits on the compose bridge. */
async function insideNetwork(python: string): Promise<string> {
  return docker(['exec', ONVIF.container, 'python3', '-c', python]);
}

/**
 * Every path the relay currently holds, and whether it is ready.
 *
 * `ready` is the difference between "the api asked for this path" and "the
 * camera is actually feeding it", which is precisely the distinction a camera
 * status journey is about.
 */
export async function relayPaths(): Promise<{ name: string; ready: boolean }[]> {
  const out = await insideNetwork(
    'import json,urllib.request;' +
    "d=json.load(urllib.request.urlopen('http://mediamtx:9987/v3/paths/list'));" +
    "print(json.dumps([{'name':i['name'],'ready':bool(i.get('ready'))} for i in d.get('items',[])]))",
  );
  return JSON.parse(out.trim());
}

export async function relayPathNames(): Promise<string[]> {
  return (await relayPaths()).map((p) => p.name);
}

/** Is this exact relay path present? Used for both halves of J9. */
export async function relayHasPath(name: string): Promise<boolean> {
  return (await relayPathNames()).includes(name);
}

/**
 * Take a camera off the network, as a power cut or a pulled cable would.
 *
 * `stop`, not `pause`: a paused container still holds its TCP connections open,
 * so the relay would not see a disconnect and the product would have nothing to
 * notice. Stopping closes the socket, which is what a real camera does.
 */
export async function stopCamera(container: string): Promise<void> {
  await docker(['stop', '-t', '2', container], 40_000);
}

export async function startCamera(container: string): Promise<void> {
  await docker(['start', container], 40_000);
}

/** Both synthetic cameras running, whatever a previous test did to them. */
export async function restoreCameras(): Promise<void> {
  for (const cam of [CAMERAS.one.container, CAMERAS.two.container]) {
    const state = await docker(['inspect', '-f', '{{.State.Running}}', cam]).catch(() => 'true\n');
    if (state.trim() !== 'true') await startCamera(cam);
  }
}

/**
 * Give a camera the sub track a probe WOULD have stored, by writing the row the
 * product's own resolver writes.
 *
 * WHY THIS CANNOT GO THROUGH `POST /cameras/{id}/sub-track/resolve`, and why
 * that is the product behaving correctly rather than a gap. `substream.judge`
 * refuses a sub whenever the MAIN stream is already browser-safe: there is no
 * transcode for a sub to remove, so a second copy of every frame on disk buys
 * nothing. The synthetic cameras are H.264 — deliberately, because the live-view
 * journeys need a stream a browser can actually decode — so the resolver will
 * always, correctly, decline to give them a sub.
 *
 * A sub track is therefore a PRECONDITION this suite has to seed, exactly as J8
 * seeds a foreign index row: below the boundary under test, so that everything
 * above it stays real. Once the row exists, the relay's own reconcile loop
 * creates the `<slug>_sub` path (`relay_tracks_for` carries a resolved sub
 * on-demand for the HEVC live-view fallback), an operator can switch its
 * recording on through `PUT /cameras/{id}/sub-track`, and deletion tears both
 * tracks down — all product code, none of it stubbed.
 *
 * The dict mirrors what `substream.resolve_detailed` returns, field for field.
 * `recording_enabled` is false here for the same reason it is false there:
 * resolution records what a camera offers, switching it on is a separate act.
 */
export async function seedResolvedSubTrack(
  slug: string,
  which: keyof typeof CAMERAS,
): Promise<string> {
  const cam = CAMERAS[which];
  const urlRaw = `rtsp://${cam.container}:8554/${cam.path}-sub`;
  const subTrack = {
    url_raw: urlRaw,
    codec: 'h264',
    width: 640,
    height: 360,
    fps: 15,
    bitrate_mbps: 0.4,
    main_bitrate_mbps: 2.0,
    source: 'onvif',
    verified: true,
    recording_enabled: false,
    probed_at: new Date().toISOString(),
  };
  await docker([
    'exec', 'vms_postgres',
    'psql', '-U', 'rtsp', '-d', 'rtsp_relay', '-v', 'ON_ERROR_STOP=1', '-c',
    `UPDATE cameras SET sub_track = '${JSON.stringify(subTrack)}'::jsonb ` +
    `WHERE slug = '${slug}'`,
  ], 30_000);
  return urlRaw;
}

/** The camera's own RTSP URL — what a real ONVIF device would advertise. */
export function cameraRtspUrl(which: keyof typeof CAMERAS): string {
  const cam = CAMERAS[which];
  return `rtsp://${cam.container}:8554/${cam.path}`;
}

/**
 * The IP the ONVIF simulator has on the compose bridge.
 *
 * Discovery finds it by itself; this is for the tests that need to name the
 * device — adding it by IP rather than waiting for a scan, which is a supported
 * product path ("Manual IP") and a much faster one.
 */
export async function onvifSimIp(): Promise<string> {
  const out = await docker([
    'inspect', '-f',
    '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}',
    ONVIF.container,
  ]);
  return out.trim();
}

/**
 * Forget the last discovery scan.
 *
 * The wizard's first step renders from the job record in Valkey, not from the
 * device list — so a stack that has ever scanned shows "0 new cameras found"
 * and a "Scan again" button instead of the initial "Start auto-discovery". A
 * test that resets the DATABASE but not this one finds a different first screen
 * depending on what ran before it, which is precisely the inherited state that
 * makes journeys order-dependent.
 *
 * The key is the one scan_manager writes (`_JOB_KEY`); the lock is left alone
 * because a scan in flight should not be torn out from under itself.
 */
export async function clearScanJob(): Promise<void> {
  await docker(['exec', 'vms_redis', 'valkey-cli', 'DEL', 'discovery:scan:job'])
    .catch(async () => {
      await docker(['exec', 'vms_redis', 'redis-cli', 'DEL', 'discovery:scan:job']);
    });
}
