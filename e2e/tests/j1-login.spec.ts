/**
 * J1 — real Keycloak login: redirect, PKCE, callback, application.
 *
 * WHAT THIS PROVES THAT NOTHING ELSE CAN. The frontend suite covers
 * AuthProvider's three states against a mocked fetch, and the API suite covers
 * token validation against a locally-signed JWT. Neither can perform a
 * full-page redirect to a different origin and come back, which is where this
 * flow's real hazards live:
 *
 *   * `responseMode: 'query'` is set because Keycloak's default `fragment`
 *     appends `#state=…&code=…` onto a hash router's URL and corrupts the
 *     callback — the token exchange then 400s with "Server responded with an
 *     invalid status". Only a real redirect can show that it is fixed.
 *   * `redirectUri` is pinned hash-free for the same reason, which is why the
 *     `vms-return` sessionStorage round-trip exists at all.
 *   * The issuer the browser produces has to be in the api's allow-list. That
 *     is a function of which host and port the browser used, so only a browser
 *     can produce the value that matters.
 */
import { expect, test } from '@playwright/test';
import { APP_READY, login, submitKeycloakForm } from '../support/auth';
import { KEYCLOAK_PORT, KEYCLOAK_URL, USERS } from '../support/env';

test.describe('J1 · login through real Keycloak', () => {
  test('an anonymous visitor is sent to Keycloak, not shown the app', async ({ page }) => {
    await page.goto('/');
    await page.waitForURL(new RegExp(`:${KEYCLOAK_PORT}/realms/vms/protocol/openid-connect/auth`), {
      timeout: 60_000,
    });
    // The application must not be behind the login form.
    await expect(page.locator('#username')).toBeVisible();
    expect(await page.locator(APP_READY).count()).toBe(0);
  });

  test('the authorization request uses PKCE with S256', async ({ page }) => {
    // Not an implementation detail: a public client without PKCE is
    // interceptable, and `pkceMethod: 'S256'` in auth.tsx is only a claim until
    // the request on the wire carries it.
    await page.goto('/');
    await page.waitForURL(/protocol\/openid-connect\/auth/, { timeout: 60_000 });

    const url = new URL(page.url());
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    expect(url.searchParams.get('code_challenge')).toBeTruthy();
    expect(url.searchParams.get('client_id')).toBe('vms-web');
    expect(url.searchParams.get('response_type')).toBe('code');
  });

  test('the callback comes back on the query string, never the fragment', async ({ page }) => {
    // THE REGRESSION THIS EXISTS FOR. With Keycloak's default response mode the
    // code arrives in the fragment, collides with the hash route, and the token
    // exchange fails. Asserting the redirect_uri is hash-free is how that stays
    // fixed.
    await page.goto('/');
    await page.waitForURL(/protocol\/openid-connect\/auth/, { timeout: 60_000 });
    const authUrl = new URL(page.url());

    const redirectUri = authUrl.searchParams.get('redirect_uri') ?? '';
    expect(redirectUri).not.toContain('#');

    const responseMode = authUrl.searchParams.get('response_mode');
    // Keycloak omits the parameter when the client's default already is query;
    // what must never appear is `fragment`.
    expect(responseMode).not.toBe('fragment');
  });

  test('a valid admin login lands in the application', async ({ page }) => {
    await login(page, 'admin');
    expect(page.url()).toContain('localhost');
    expect(page.url()).not.toContain(`:${KEYCLOAK_PORT}`);
    await expect(page.locator(APP_READY).first()).toBeVisible();
  });

  test('the application shows who is signed in', async ({ page }) => {
    // ASSERTED ON SCREEN, not through /api/me.
    //
    // A raw fetch() from page.evaluate carries no Authorization header — the
    // SPA attaches the bearer inside lib/api.ts, so a hand-rolled request
    // bypasses it and 401s. That is a fact about the test, not the product, and
    // reaching for the API here would also break the rule that a user-visible
    // assertion beats an implementation one. The shell renders the username; a
    // session that failed to resolve a principal cannot render it.
    await login(page, 'admin');
    await expect(page.getByText(USERS.admin.username, { exact: true }).first())
      .toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('Administrator').first()).toBeVisible();
  });

  test('the api accepts the browser\'s token, so the issuer pin matches', async ({ page }) => {
    // THE SUBTLEST MISCONFIGURATION THIS STACK CAN HAVE: the browser reaches
    // Keycloak on one origin, the api pins `iss` from another, and every call
    // 401s while the session looks perfectly valid.
    //
    // The proof is a page that renders data it could only have fetched with an
    // accepted token. Camera Inventory lists cameras from GET /api/cameras; if
    // the pin were wrong the app would surface an error instead.
    const failures: number[] = [];
    page.on('response', (r) => {
      if (r.url().includes('/api/') && r.status() === 401) failures.push(r.status());
    });
    await login(page, 'admin');
    await page.goto('/#/cameras');
    await expect(page.getByRole('heading', { name: /Camera Inventory/i }).or(
      page.getByText(/Camera Inventory/i)).first()).toBeVisible({ timeout: 30_000 });
    expect(failures, 'an authenticated page made a request the api refused').toEqual([]);
  });

  // The non-admin sign-in test lives in identity.spec.ts: it needs the seeded
  // `operator`, which only the realm shipped with the identity extension has.

  test('the session survives a reload without going back to Keycloak', async ({ page }) => {
    await login(page, 'admin');
    await page.reload();
    await expect(page.locator(APP_READY).first()).toBeVisible({ timeout: 60_000 });
    expect(page.url()).not.toContain('/protocol/openid-connect/auth');
  });

  test('a refresh on a deep route returns to that route, not the default', async ({ page }) => {
    // What `vms-return` is for: the redirect URI is pinned hash-free, so
    // Keycloak's round-trip drops the #/route. Without the sessionStorage
    // restore every refresh lands on the default page.
    await login(page, 'admin');
    await page.goto('/#/cameras');
    await expect(page).toHaveURL(/#\/cameras/);
    await page.reload();
    await expect(page.locator(APP_READY).first()).toBeVisible({ timeout: 60_000 });
    await expect(page).toHaveURL(/#\/cameras/, { timeout: 30_000 });
  });

  test('Keycloak is a separate origin, and the app is not framing it', async ({ page }) => {
    await page.goto('/');
    await page.waitForURL(/protocol\/openid-connect\/auth/, { timeout: 60_000 });
    // A login rendered inside the app's own page would mean credentials typed
    // into a document the app controls.
    expect(page.url().startsWith(KEYCLOAK_URL)).toBe(true);
    expect(page.frames().length).toBe(1);
  });

  test('the login form rejects an empty submission before any redirect', async ({ page }) => {
    await page.goto('/');
    await submitKeycloakForm(page, '', '');
    // Still on Keycloak: it must not hand out a session for nothing.
    await expect(page.locator('#username')).toBeVisible();
    expect(page.url()).toContain('/realms/vms/');
  });
});
