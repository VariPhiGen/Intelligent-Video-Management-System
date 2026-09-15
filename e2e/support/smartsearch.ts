/**
 * smartsearch.ts — seeding the shared index, including rows this VMS must never
 * be allowed to see.
 *
 * WHY SEEDING GOES DIRECT AND SEARCHING GOES THROUGH THE PRODUCT. The whole
 * point of J8 is the boundary between the two. The index is shared with the
 * analytics appliance and holds rows from cameras this VMS does not record;
 * `services/search_scope.py` is the only thing keeping those off an operator's
 * screen. To test that, the suite has to be able to put a foreign row INTO the
 * index — something no product endpoint offers, and rightly so.
 *
 * So observations are written with SmartSearch's own `POST /observations`, the
 * real ingest path the analytics service uses, reached from inside the compose
 * network. Reads then go through `/api/search/*` on the api, exactly as the
 * browser does. Seeding below the boundary and reading above it is what makes
 * the assertion meaningful.
 *
 * SmartSearch has no host port on purpose, so every call here is a `docker
 * exec` into a container that already sits on the bridge.
 */
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { ONVIF } from './env';

const run = promisify(execFile);

/** SmartSearch as seen from inside the compose network. */
const SS = 'http://smartsearch:8013';

async function inNetwork(python: string, timeoutMs = 60_000): Promise<string> {
  const { stdout } = await run(
    'docker', ['exec', ONVIF.container, 'python3', '-c', python],
    { timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024 },
  );
  return stdout.trim();
}

/**
 * Register a camera in SmartSearch's own registry.
 *
 * Separate from registering it in the VMS. A camera the VMS owns is registered
 * in both; the FOREIGN camera used by J8 is registered only here, which is
 * precisely the shape of a row written by the other appliance.
 */
export async function registerIndexCamera(
  name: string,
  domains: string[] = ['person'],
): Promise<any> {
  const q = domains.map((d) => `('domains', '${d}')`).join(', ');
  const out = await inNetwork(
    'import json,urllib.parse,urllib.request\n' +
    `params=urllib.parse.urlencode([('rtsp_url','rtsp://synthetic/${name}'), ${q}])\n` +
    `req=urllib.request.Request('${SS}/cameras/${name}?'+params, method='POST')\n` +
    'print(urllib.request.urlopen(req).read().decode())',
  );
  return JSON.parse(out);
}

/**
 * Write one observation through SmartSearch's real ingest endpoint.
 *
 * The crop is a solid-colour PNG generated in the container. Its CONTENT does
 * not matter to this journey — what matters is that a row exists for `camera`
 * and that the api's scoping decides whether it may be seen. The colour just
 * makes two seeded rows visibly different if anyone opens one.
 *
 * PNG on the wire because the endpoint says so: the crop is embedded before it
 * is saved, so a lossy hop here would change the vector and therefore the
 * dedup decision.
 */
export async function seedObservation(opts: {
  camera: string;
  ts?: number;
  domain?: string;
  label?: string;
  confidence?: number;
  colour?: [number, number, number];
}): Promise<any> {
  const {
    camera,
    ts = Date.now() / 1000,
    domain = 'person',
    label = 'person',
    confidence = 0.95,
    colour = [30, 92, 123],
  } = opts;

  const python = `
import io, json, struct, zlib, urllib.request, uuid

def png(w, h, rgb):
    raw = b''.join(b'\\x00' + bytes(rgb) * w for _ in range(h))
    def chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    return (b'\\x89PNG\\r\\n\\x1a\\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))

crop = png(64, 128, (${colour[0]}, ${colour[1]}, ${colour[2]}))
meta = json.dumps({
    "camera": ${JSON.stringify(camera)},
    "ts": ${ts},
    "domain": ${JSON.stringify(domain)},
    "label": ${JSON.stringify(label)},
    "confidence": ${confidence},
    "bbox": [0.1, 0.1, 0.5, 0.9],
})

boundary = '----e2e' + uuid.uuid4().hex
body = b''
for name, value in (('meta', meta.encode()),):
    body += ('--%s\\r\\nContent-Disposition: form-data; name="%s"\\r\\n\\r\\n' % (boundary, name)).encode()
    body += value + b'\\r\\n'
body += ('--%s\\r\\nContent-Disposition: form-data; name="crop"; filename="c.png"\\r\\n'
         'Content-Type: image/png\\r\\n\\r\\n' % boundary).encode()
body += crop + b'\\r\\n'
body += ('--%s--\\r\\n' % boundary).encode()

req = urllib.request.Request('${SS}/observations', data=body, method='POST')
req.add_header('Content-Type', 'multipart/form-data; boundary=' + boundary)
try:
    print(urllib.request.urlopen(req).read().decode())
except urllib.error.HTTPError as e:
    print(json.dumps({"error": e.code, "detail": e.read().decode()[:300]}))
`;
  return JSON.parse(await inNetwork(python));
}

/** What the index itself holds for a domain, ignoring any VMS scoping. */
export async function indexStats(domain = 'person'): Promise<any> {
  const out = await inNetwork(
    'import json,urllib.request\n' +
    `print(urllib.request.urlopen('${SS}/${domain}/stats').read().decode())`,
  );
  return JSON.parse(out);
}

/**
 * Ask the INDEX directly for its detections feed, unscoped.
 *
 * Used only to prove that a foreign row really is in the index — which is what
 * makes "the product did not show it" mean something. Without this the test
 * could pass because the row was never written.
 */
export async function indexDetections(sinceMs = 0, limit = 200): Promise<any> {
  const out = await inNetwork(
    'import json,urllib.request\n' +
    `print(urllib.request.urlopen('${SS}/detections?since_ms=${sinceMs}&limit=${limit}').read().decode())`,
  );
  return JSON.parse(out);
}
