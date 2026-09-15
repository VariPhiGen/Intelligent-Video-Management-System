/**
 * global-setup.ts — refuse to run against a stack that is not there.
 *
 * WHAT THIS IS FOR. Playwright's own `webServer` cannot express "fourteen
 * containers, migrations applied, a realm imported, two cameras serving RTSP",
 * so readiness lives in e2e/scripts/up.sh. What is left is making the failure
 * mode good: without this, running the suite against a stopped stack produces
 * a wall of navigation timeouts and a developer reading trace files to discover
 * that nothing was listening.
 *
 * So this asks four questions, and each one names its own fix. It is not a
 * readiness WAIT — up.sh already did that — it is a diagnosis.
 */
import { request } from '@playwright/test';
import { keycloakAdminToken } from './api';
import { API_PORT, BASE_URL, KEYCLOAK_URL, REALM, USERS } from './env';
import { BAD_LOGIN_USER, clearLoginFailures, ensureBadLoginUser } from './keycloak';
import { resetAppliance } from './reset';

function fail(what: string, detail: string): never {
  throw new Error(
    `\n\nE2E stack is not usable: ${what}\n` +
    `  ${detail}\n\n` +
    `  Start it with:   e2e/scripts/up.sh\n` +
    `  Tear it down:    e2e/scripts/down.sh -v\n`,
  );
}

export default async function globalSetup() {
  const ctx = await request.newContext({ timeout: 15_000 });
  try {
    // 1. Is the app being served at all?
    let resp;
    try {
      resp = await ctx.get(`${BASE_URL}/health`);
    } catch (e: any) {
      fail('the api is not answering',
           `${BASE_URL}/health — ${e.message.split('\n')[0]}`);
    }
    if (!resp.ok()) fail('the api is unhealthy', `${BASE_URL}/health -> ${resp.status()}`);

    // 2. Is it running with REAL Keycloak? A stack left in dev-auth mode would
    //    make every authorisation journey pass without proving anything, which
    //    is the single most dangerous way for this suite to be wrong.
    const cfg = await (await ctx.get(`${BASE_URL}/api/auth/config`)).json();
    if (cfg.mode !== 'oidc') {
      fail('the stack is in DEV_AUTH mode, not OIDC',
           `/api/auth/config returned mode=${cfg.mode!} — every login journey ` +
           `would pass without exercising Keycloak. Set DEV_AUTH=false.`);
    }
    if (String(cfg.port) !== String(new URL(KEYCLOAK_URL).port)) {
      fail('the api and the tests disagree about Keycloak',
           `api says port ${cfg.port}, tests expect ${new URL(KEYCLOAK_URL).port}`);
    }

    // 3. Is the realm importable-and-imported? A missing realm shows up much
    //    later as a redirect to a 404 page that looks like a login failure.
    const wk = await ctx.get(
      `${KEYCLOAK_URL}/realms/${REALM}/.well-known/openid-configuration`,
    );
    if (!wk.ok()) fail('the vms realm is missing', `${KEYCLOAK_URL}/realms/${REALM} -> ${wk.status()}`);

    // 4. Does the seeded admin exist, with the admin role?
    //
    //    NOT checked with a password grant. `vms-web` is a public client with
    //    directAccessGrants disabled — correctly, since enabling them would let
    //    anyone trade a password for a token without a client secret. So this
    //    asks Keycloak's admin API instead, using the bootstrap admin on the
    //    master realm, which is the same grant the product's own
    //    keycloak_admin.py uses. Whether the user can actually SIGN IN is what
    //    J1 is for; this only catches a realm that imported without its users.
    let adminToken: string;
    try {
      adminToken = await keycloakAdminToken();
    } catch (e: any) {
      fail('the Keycloak bootstrap admin is not usable', e.message.split('\n')[0]);
    }
    const users = await ctx.get(
      `${KEYCLOAK_URL}/admin/realms/${REALM}/users?username=${USERS.admin.username}&exact=true`,
      { headers: { Authorization: `Bearer ${adminToken}` } },
    );
    if (!users.ok()) {
      fail('cannot read the realm\'s users', `-> ${users.status()}`);
    }
    const found = await users.json();
    if (!Array.isArray(found) || found.length === 0) {
      fail(`the realm has no user "${USERS.admin.username}"`,
           'the realm imported without its seeded users');
    }

    // The account J2 is allowed to lock, and a clean attack-detection slate for
    // the accounts it is not. Keycloak's quick-login check locks a user for a
    // minute after two attempts inside a second, WITHOUT incrementing any
    // counter — so a previous run's failures can silently decide this one's
    // outcome unless they are cleared here.
    await ensureBadLoginUser();
    for (const u of [USERS.admin.username, USERS.operator.username, BAD_LOGIN_USER.username]) {
      await clearLoginFailures(u);
    }

    // A run starts from a known appliance. Leftovers from an interrupted run
    // are the classic reason a suite passes locally and fails in CI.
    await resetAppliance();

    console.log(`▸ E2E stack verified on :${API_PORT} (oidc, realm ${REALM})`);
  } finally {
    await ctx.dispose();
  }
}
