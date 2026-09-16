/**
 * env.ts — one place that knows where the stack is and who can log in.
 *
 * Values are read from e2e/e2e.env rather than duplicated, so moving a port
 * moves it for the stack, the readiness script and the tests together. A test
 * suite carrying its own copy of a port number is a suite that passes against
 * the wrong stack.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// `__dirname`, not `import.meta.url`: Playwright transpiles these files to
// CommonJS (package.json declares no "type": "module"), so the ESM-only form
// throws "exports is not defined in ES module scope" before a single test runs.
// E2E_ENV_FILE is the escape hatch for pointing a run at another stack.
const ENV_FILE = process.env.E2E_ENV_FILE ?? resolve(__dirname, '..', 'e2e.env');

function readEnvFile(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of readFileSync(ENV_FILE, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    if (eq > 0) out[t.slice(0, eq).trim()] = t.slice(eq + 1).trim();
  }
  return out;
}

const FILE = readEnvFile();
const get = (k: string, fallback: string) => process.env[k] ?? FILE[k] ?? fallback;

export const API_PORT = get('API_PORT', '19091');
export const KEYCLOAK_PORT = get('KEYCLOAK_PORT', '19085');

export const BASE_URL = process.env.E2E_BASE_URL ?? `http://localhost:${API_PORT}`;
export const KEYCLOAK_URL = `http://localhost:${KEYCLOAK_PORT}`;
export const REALM = 'vms';

/** The internal key, for seeding through the service principal where a test
 *  needs state rather than a journey through the UI. */
export const INTERNAL_API_KEY = get('INTERNAL_API_KEY', '');

/** Users. `admin` comes from the product's own realm import. `operator` exists
 *  only where the realm file still seeds it — the public tree's realm seeds
 *  admin alone, and identity.spec.ts, the only spec that signs in as operator,
 *  is stripped with it. Anything else a journey needs is created through the
 *  product's user API, which is a path worth exercising rather than a fixture
 *  to fake. */
export const USERS = {
  admin: { username: get('VMS_ADMIN_USER', 'admin'), password: get('VMS_ADMIN_PASSWORD', 'admin') },
  operator: { username: 'operator', password: 'operator' },
} as const;

/** The ONVIF simulator's credentials — the "right password" for J3, and the
 *  thing a wrong-password test has to differ from. */
export const ONVIF = {
  user: get('E2E_ONVIF_USER', 'admin'),
  password: get('E2E_ONVIF_PASS', 'admin123'),
  /** Where the simulator lives on the compose bridge. Discovery finds this by
   *  itself; a test that needs to name it uses the container name. */
  container: 'vms-e2e-onvif-sim',
  vendor: 'VariPhi E2E',
  model: 'SimCam-1000',
} as const;

export const CAMERAS = {
  one: { container: 'vms-e2e-rtsp-cam-1', path: 'e2e-cam-1', profile: '16x9' },
  two: { container: 'vms-e2e-rtsp-cam-2', path: 'e2e-cam-2', profile: '4x3' },
} as const;

export const COMPOSE_PROJECT = process.env.E2E_PROJECT ?? 'vms-e2e';

/** The Keycloak BOOTSTRAP admin (master realm), not a product user.
 *  Used only to interrogate Keycloak itself — never to act as a VMS user. */
export const KEYCLOAK_ADMIN = {
  username: get('KEYCLOAK_ADMIN', 'admin'),
  password: get('KEYCLOAK_ADMIN_PASSWORD', ''),
} as const;
