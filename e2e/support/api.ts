/**
 * api.ts — talking to the product's API from a test, for setup and for
 * assertions the UI cannot make.
 *
 * TWO PRINCIPALS, AND THE CHOICE IS DELIBERATE EACH TIME.
 *
 * `service()` uses X-Internal-Key. It is trusted for everything, so it is the
 * right tool for arranging state a journey needs but is not itself about —
 * wiping cameras between tests, seeding an observation. Using it for an
 * assertion would be cheating: it bypasses exactly the authorisation the
 * product is being tested for.
 *
 * `asUser()` carries a real Keycloak token obtained by a real password grant.
 * Anything checking what a ROLE can do goes through this, because the answer
 * has to come from the same code path a browser would hit.
 *
 * WHY ASSERT THROUGH THE API AT ALL, when the rule is to prefer user-visible
 * assertions? Because some product facts have no pixels: that a MediaMTX path
 * exists, that a camera's sub track was torn down, that a search hit was scoped
 * out rather than merely scrolled past. The rule is to prefer a UI assertion
 * where one is POSSIBLE — not to pretend the UI is the only surface.
 */
import { request, type APIRequestContext } from '@playwright/test';
import { BASE_URL, INTERNAL_API_KEY, KEYCLOAK_ADMIN, KEYCLOAK_URL } from './env';

/** A client authenticated as the trusted internal service. Setup only. */
export async function service(): Promise<APIRequestContext> {
  return request.newContext({
    baseURL: BASE_URL,
    extraHTTPHeaders: { 'X-Internal-Key': INTERNAL_API_KEY },
  });
}

/** An unauthenticated client, for asserting that something is refused. */
export async function anonymous(): Promise<APIRequestContext> {
  return request.newContext({ baseURL: BASE_URL });
}

/**
 * THERE IS NO PASSWORD GRANT FOR A USER, and that is the product being right.
 *
 * The obvious way to get a user's token in a test is a direct access grant
 * against `vms-web`. Keycloak refuses it: the realm ships that client with
 * `directAccessGrantsEnabled: false` because it is PUBLIC — enabling password
 * grants on it would let anyone on the network trade a username and password
 * for a token, with no client secret in the way. The only client that does
 * allow the grant is `vms-pwcheck`, which is confidential and exists solely so
 * a signed-in user can prove they know their CURRENT password before changing
 * it (see keycloak_admin.verify_password).
 *
 * So a user token comes from a real browser login, and nowhere else. That is
 * slower and it is the right shape: the journeys are supposed to exercise the
 * flow, not route around it.
 */
export async function tokenFromBrowser(page: {
  evaluate: (fn: () => any) => Promise<any>;
}): Promise<string> {
  const token = await page.evaluate(() =>
    // keycloak-js keeps the live token on the adapter it created; the SPA holds
    // exactly one, and this is the same value every fetch from the app carries.
    (window as any).__vmsKeycloakToken ?? null);
  if (token) return token as string;
  throw new Error(
    'No browser token available. Use page.evaluate(fetch) to call the API as ' +
    'the signed-in user instead — the SPA attaches the bearer itself, so a ' +
    'request made from the page is already authenticated as that principal.',
  );
}

/**
 * An admin-realm token, the way the product's own keycloak_admin.py gets one.
 *
 * `admin-cli` on the MASTER realm does allow a password grant — that is the
 * bootstrap admin, not a product user. Used only to inspect Keycloak itself
 * (does this user exist, what roles does it hold), never to impersonate a user
 * against the api.
 */
export async function keycloakAdminToken(): Promise<string> {
  const ctx = await request.newContext();
  try {
    const resp = await ctx.post(
      `${KEYCLOAK_URL}/realms/master/protocol/openid-connect/token`,
      {
        form: {
          grant_type: 'password',
          client_id: 'admin-cli',
          username: KEYCLOAK_ADMIN.username,
          password: KEYCLOAK_ADMIN.password,
        },
      },
    );
    if (!resp.ok()) {
      throw new Error(
        `Keycloak refused the bootstrap admin: ${resp.status()} ` +
        `${(await resp.text()).slice(0, 200)}`,
      );
    }
    return (await resp.json()).access_token as string;
  } finally {
    await ctx.dispose();
  }
}

/** Every registered camera, as the service principal sees them. */
export async function listCameras(ctx: APIRequestContext): Promise<any[]> {
  const resp = await ctx.get('/api/cameras');
  if (!resp.ok()) throw new Error(`GET /api/cameras -> ${resp.status()}`);
  return resp.json();
}

// The relay's path table is NOT reachable through the api: there is no
// endpoint for it, and MediaMTX's control port is deliberately unpublished.
// Reading it is therefore a stack-level operation — see support/stack.ts.
