/**
 * J2 — what happens when authentication does not succeed.
 *
 * THE HALF NOBODY EXERCISES. A login flow gets used every day, so its happy
 * path is proven by usage. The refusals are not: a wrong password, a session
 * that ends, a direct navigation to a protected route by someone who never
 * signed in. Those are the paths an attacker takes and the paths a user hits on
 * their worst day, and until this file none of them had a test at any level.
 *
 * THE STRONGEST ASSERTION HERE is that refusal happens at the API, not merely
 * in the interface. A nav item that is hidden is a courtesy; a route that
 * answers 401 is the control. J10 will cover the role matrix — this file covers
 * the one below it: no identity at all.
 */
import { expect, test } from '@playwright/test';
import { anonymous } from '../support/api';
import { APP_READY, login, submitKeycloakForm } from '../support/auth';
import { KEYCLOAK_PORT, REALM, USERS } from '../support/env';
import { BAD_LOGIN_USER, clearLoginFailures } from '../support/keycloak';

test.describe('J2 · authentication failure and session behaviour', () => {

  // EVERY BAD PASSWORD GOES TO A THROWAWAY ACCOUNT. Keycloak locks a user for a
  // minute after two attempts inside a second (the quick-login check), so
  // aiming failures at `admin` would lock the account the rest of the suite
  // signs in with, and the resulting failures would appear in unrelated tests.
  test.afterEach(async () => {
    await clearLoginFailures(BAD_LOGIN_USER.username);
  });

  // ── Bad credentials ──────────────────────────────────────────────────────

  test('a wrong password is refused and says so', async ({ page }) => {
    await page.goto('/');
    await submitKeycloakForm(page, BAD_LOGIN_USER.username, 'not-the-password');

    // Keycloak re-renders its own form with an error. The user must be told,
    // not silently returned to an empty form.
    await expect(page.locator('#username')).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByText(/invalid|incorrect|try again/i).first(),
    ).toBeVisible({ timeout: 20_000 });
    expect(page.url()).toContain(`:${KEYCLOAK_PORT}`);
  });

  test('a wrong password does not admit the application', async ({ page }) => {
    await page.goto('/');
    await submitKeycloakForm(page, BAD_LOGIN_USER.username, 'not-the-password');
    await expect(page.locator('#username')).toBeVisible({ timeout: 30_000 });
    expect(await page.locator(APP_READY).count()).toBe(0);
  });

  test('an unknown username is refused the same way as a wrong password', async ({ page }) => {
    // Deliberately the same assertion: a login form that distinguishes "no such
    // user" from "wrong password" is a username oracle.
    await page.goto('/');
    await submitKeycloakForm(page, 'nobody-by-that-name', 'whatever');
    await expect(page.locator('#username')).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByText(/invalid|incorrect|try again/i).first(),
    ).toBeVisible({ timeout: 20_000 });
  });

  test('a failed attempt can be followed by a successful one', async ({ page }) => {
    // A login form that wedges after one mistake is a lockout the product did
    // not ask for.
    //
    // THIS TEST CAUGHT A HARNESS BUG, NOT A PRODUCT ONE, and the story is worth
    // keeping. It first failed while the bad-password journeys aimed their
    // failures at `admin` — the same account every other test signs in with.
    // Keycloak's quick-login check locks a user for a minute when two attempts
    // arrive inside a second, WITHOUT incrementing numFailures, so the account
    // read as perfectly healthy while refusing a correct password. The symptom
    // appeared here; the cause was three tests earlier.
    //
    // The fix is structural: failures go to a throwaway account (BAD_LOGIN_USER)
    // and its attack-detection state is cleared after every test. Anything that
    // starts pointing bad passwords at a shared account will bring the mystery
    // straight back.
    await page.goto('/');
    await submitKeycloakForm(page, BAD_LOGIN_USER.username, 'wrong-first-time');
    await expect(page.locator('#username')).toBeVisible({ timeout: 30_000 });

    await submitKeycloakForm(page, BAD_LOGIN_USER.username, BAD_LOGIN_USER.password, false);
    // BAD_LOGIN_USER holds no roles, so it reaches the app rather than the
    // login form — which is all this test is about. What it can DO once inside
    // is J10's subject.
    await page.waitForURL((u) => !u.href.includes('/protocol/openid-connect/auth'),
                          { timeout: 60_000 });
    expect(page.url()).not.toContain(`:${KEYCLOAK_PORT}`);
  });

  // "The lockout is per-account" lives in identity.spec.ts: proving it needs a
  // second account to sign in as, and the public realm seeds admin alone.

  // ── No identity at all ───────────────────────────────────────────────────

  test('every protected route refuses an anonymous caller at the API', async () => {
    // THE CONTROL, as distinct from the courtesy. Hiding a nav item is not
    // access control; answering 401 is.
    const api = await anonymous();
    try {
      for (const path of [
        '/api/cameras',
        '/api/me',
        // '/api/users' is checked in identity.spec.ts — it is mounted only
        // where the identity extension is present.
        '/api/audit',
        '/api/discovery/devices',
        '/api/search/cameras',
        '/api/sitemaps',
      ]) {
        const resp = await api.get(path);
        expect(resp.status(), `${path} served an anonymous caller`).toBe(401);
      }
    } finally {
      await api.dispose();
    }
  });

  test('a 401 tells the client how to authenticate', async () => {
    // The SPA drives its login off this header; without it an expired session
    // reads as a generic failure rather than "sign in again".
    const api = await anonymous();
    try {
      const resp = await api.get('/api/cameras');
      expect(resp.status()).toBe(401);
      expect(resp.headers()['www-authenticate']).toBe('Bearer');
    } finally {
      await api.dispose();
    }
  });

  test('a made-up bearer token is refused', async () => {
    const api = await anonymous();
    try {
      const resp = await api.get('/api/cameras', {
        headers: { Authorization: 'Bearer not.a.real.token' },
      });
      expect(resp.status()).toBe(401);
    } finally {
      await api.dispose();
    }
  });

  test('navigating straight to a deep route still requires signing in', async ({ page }) => {
    // Someone pasting a link into a fresh browser must not skip the gate.
    await page.goto('/#/admin');
    await page.waitForURL(new RegExp(`:${KEYCLOAK_PORT}/realms/${REALM}/protocol`), {
      timeout: 60_000,
    });
    expect(await page.locator(APP_READY).count()).toBe(0);
  });

  test('the public auth config is readable before login, and says nothing secret', async () => {
    // The SPA must read this BEFORE it can sign in, so it is deliberately
    // public — which makes what it contains worth pinning.
    const api = await anonymous();
    try {
      const resp = await api.get('/api/auth/config');
      expect(resp.status()).toBe(200);
      const cfg = await resp.json();
      expect(cfg.mode).toBe('oidc');
      expect(Object.keys(cfg).sort()).toEqual(
        ['client_id', 'mode', 'port', 'realm', 'url'],
      );
      // No secret has any business here.
      expect(JSON.stringify(cfg)).not.toMatch(/secret|password|key/i);
    } finally {
      await api.dispose();
    }
  });

  // ── Ending a session ─────────────────────────────────────────────────────

  test('signing out ends the session and returns to the login form', async ({ page }) => {
    await login(page, 'admin');
    await expect(page.locator(APP_READY).first()).toBeVisible();

    // The control is an icon button identified by its title, which is also
    // what a screen-reader user hears.
    await page.getByTitle('Sign out').click();

    // Back at Keycloak, and the application is gone.
    await page.waitForURL(
      new RegExp(`:${KEYCLOAK_PORT}/realms/${REALM}/protocol/openid-connect/auth`),
      { timeout: 60_000 },
    );
    await expect(page.locator('#username')).toBeVisible();
  });

  test('after signing out, going back does not restore the session', async ({ page }) => {
    // The browser-history case: a logged-out session that a Back button
    // resurrects is a shared-workstation problem.
    await login(page, 'admin');
    await page.getByTitle('Sign out').click();
    await page.waitForURL(/protocol\/openid-connect\/auth/, { timeout: 60_000 });

    await page.goto('/#/cameras');
    await page.waitForURL(/protocol\/openid-connect\/auth/, { timeout: 60_000 });
    expect(await page.locator(APP_READY).count()).toBe(0);
  });

  test('a session whose tokens are discarded cannot keep using the app', async ({ page }) => {
    // Approximates an expired session: the SPA's adapter loses its tokens, and
    // the next thing it does must be to send the user back to Keycloak rather
    // than render a broken page or a silently empty one.
    await login(page, 'admin');
    await page.evaluate(() => {
      try { sessionStorage.clear(); localStorage.clear(); } catch { /* storage off */ }
    });
    await page.reload();

    // Either the app recovers via a live Keycloak SSO cookie, or it sends the
    // user to sign in. What it must never do is sit on a broken shell.
    await Promise.race([
      page.locator(APP_READY).first().waitFor({ state: 'visible', timeout: 60_000 }),
      page.waitForURL(/protocol\/openid-connect\/auth/, { timeout: 60_000 }),
    ]);
    const recovered = (await page.locator(APP_READY).count()) > 0;
    const atLogin = page.url().includes('/protocol/openid-connect/auth');
    expect(recovered || atLogin,
      'the app neither recovered nor asked the user to sign in').toBe(true);
  });
});
