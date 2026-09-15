/**
 * auth.ts — signing in through the real Keycloak, in a real browser.
 *
 * THE TEMPTING SHORTCUT, AND WHY IT IS REFUSED. It would be easy to fetch a
 * token with a password grant, drop it into storage and skip the redirect. That
 * would make every journey faster and would delete the only coverage the
 * product has of the thing most likely to break: a full-page redirect to a
 * different origin, PKCE S256, `responseMode: query` chosen because Keycloak's
 * default `fragment` corrupts a hash router's URL, and a `vms-return`
 * sessionStorage round-trip that survives it. None of that is reachable from
 * jsdom and none of it is exercised by a token handed over the back fence.
 *
 * So `login()` types into Keycloak's own form and follows the redirect back.
 *
 * SESSION REUSE IS STILL POSSIBLE, and it is done the honest way: Playwright's
 * storageState, captured once per role after a real login. A journey that is
 * not ABOUT logging in restores that state instead of repeating the form, which
 * keeps the suite quick without pretending the flow does not exist.
 */
import { expect, type Browser, type Page } from '@playwright/test';
import { BASE_URL, KEYCLOAK_PORT, USERS } from './env';

export type Role = keyof typeof USERS;

/** Keycloak's login form, identified the way a user would find it. */
const KC_USERNAME = '#username';
const KC_PASSWORD = '#password';
const KC_SUBMIT = '#kc-login, input[type="submit"], button[type="submit"]';

/** True once the SPA has settled past its splash and mounted the shell. */
export const APP_READY = 'nav a, [data-testid="app-shell"], aside nav';

/**
 * Go to the app and complete a real login as `role`.
 *
 * Deliberately asserts the redirect actually happened: a test that silently
 * skipped Keycloak — because a session cookie was still live, say — would
 * otherwise report success for a flow it never ran.
 */
export async function login(page: Page, role: Role, opts: { expectRedirect?: boolean } = {}) {
  const { username, password } = USERS[role];
  await page.goto('/');
  await submitKeycloakForm(page, username, password, opts.expectRedirect ?? true);
  await expect(page.locator(APP_READY).first()).toBeVisible({ timeout: 60_000 });
}

/**
 * Fill in whatever Keycloak login form is on screen.
 *
 * Split out from `login` so a failure journey can drive the form with bad
 * credentials and assert on what Keycloak says, without also asserting that the
 * app came up.
 */
export async function submitKeycloakForm(
  page: Page,
  username: string,
  password: string,
  expectRedirect = true,
) {
  if (expectRedirect) {
    await page.waitForURL(new RegExp(`:${KEYCLOAK_PORT}/realms/vms/protocol/openid-connect/auth`), {
      timeout: 60_000,
    });
  }
  await page.locator(KC_USERNAME).waitFor({ state: 'visible', timeout: 30_000 });
  await page.locator(KC_USERNAME).fill(username);
  await page.locator(KC_PASSWORD).fill(password);
  await page.locator(KC_SUBMIT).first().click();
}

/** Is the browser currently sitting on Keycloak rather than the app? */
export function onKeycloak(page: Page): boolean {
  return page.url().includes(`:${KEYCLOAK_PORT}/realms/`);
}

/** Is the browser on the application origin? */
export function onApp(page: Page): boolean {
  return page.url().startsWith(BASE_URL);
}

/**
 * Log in once and hand back the storage state, for journeys that need a session
 * but are not testing how one is obtained.
 */
export async function captureSession(browser: Browser, role: Role) {
  const context = await browser.newContext();
  const page = await context.newPage();
  await login(page, role);
  const state = await context.storageState();
  await context.close();
  return state;
}
