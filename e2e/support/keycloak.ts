/**
 * keycloak.ts — managing the realm's own state, for tests that abuse it.
 *
 * WHY THIS EXISTS AT ALL. J2 has to type wrong passwords, and Keycloak has two
 * separate anti-automation mechanisms that both bite a test suite:
 *
 *   failureFactor: 5        five bad attempts and the account is locked for up
 *                           to maxFailureWaitSeconds (900 here).
 *   quick-login check       TWO attempts inside quickLoginCheckMilliSeconds
 *                           (1s by default) lock the account for
 *                           minimumQuickLoginWaitSeconds (60s) — and this one
 *                           does NOT increment numFailures, so an account can
 *                           be locked while every counter reads zero.
 *
 * The second is the nastier one and it is what a fast E2E suite trips
 * constantly: a failed login followed immediately by a correct one is exactly
 * the shape of a brute-force attempt, and Keycloak is right to refuse it. A
 * test that does not know this reads the refusal as "correct password
 * rejected" and looks like a product bug.
 *
 * TWO CONSEQUENCES, both handled here rather than worked around with sleeps:
 *
 *   1. Bad-password journeys use a THROWAWAY user, never `admin` or
 *      `operator`. Locking a dedicated account harms nothing; locking the
 *      account the rest of the suite signs in with turns one test's failure
 *      into six.
 *   2. That user's attack-detection state is cleared between runs, so a
 *      previous run cannot decide this one's outcome.
 */
import { request } from '@playwright/test';
import { keycloakAdminToken } from './api';
import { KEYCLOAK_URL, REALM } from './env';

/** The account J2 is allowed to lock. Never used for a successful journey. */
export const BAD_LOGIN_USER = {
  username: 'e2e-badlogin',
  password: 'e2e-badlogin-correct-pw',
} as const;

// ABSOLUTE URLS, NOT A baseURL. Playwright follows the URL spec when joining,
// so a path beginning with "/" REPLACES the whole path of the base — making
// `/users` against a base of `.../admin/realms/vms` resolve to
// `http://keycloak/users`, which 404s with a message about a missing target
// resource method rather than anything about the path being wrong.
const ADMIN = `${KEYCLOAK_URL}/admin/realms/${REALM}`;

async function adminCtx() {
  const token = await keycloakAdminToken();
  return request.newContext({
    extraHTTPHeaders: { Authorization: `Bearer ${token}` },
  });
}

export async function findUserId(username: string): Promise<string | null> {
  const ctx = await adminCtx();
  try {
    const resp = await ctx.get(`${ADMIN}/users?username=${encodeURIComponent(username)}&exact=true`);
    if (!resp.ok()) return null;
    const rows = await resp.json();
    return Array.isArray(rows) && rows.length ? rows[0].id : null;
  } finally {
    await ctx.dispose();
  }
}

/**
 * Make sure the throwaway account exists with a known password.
 *
 * Idempotent: a run that follows another finds it already there. It is given no
 * realm roles at all, which is deliberate — nothing in the suite should be able
 * to use it for anything except failing to log in.
 */
export async function ensureBadLoginUser(): Promise<void> {
  const existing = await findUserId(BAD_LOGIN_USER.username);
  const ctx = await adminCtx();
  try {
    let id = existing;
    if (!id) {
      const created = await ctx.post(`${ADMIN}/users`, {
        data: {
          username: BAD_LOGIN_USER.username,
          enabled: true,
          // A COMPLETE PROFILE, DELIBERATELY. The realm runs VERIFY_PROFILE, so
          // a user missing an email is bounced to a required-action page on
          // first login — the account authenticates and still does not reach
          // the app. That is the same "Account is not fully set up" condition
          // keycloak_admin.verify_password has to tell apart from a wrong
          // password; here it would just make the fixture useless.
          email: 'e2e-badlogin@example.invalid',
          emailVerified: true,
          firstName: 'E2E',
          lastName: 'BadLogin',
          requiredActions: [],
        },
      });
      if (!created.ok() && created.status() !== 409) {
        throw new Error(
          `could not create ${BAD_LOGIN_USER.username}: ` +
          `${created.status()} ${(await created.text()).slice(0, 200)}`,
        );
      }
      id = await findUserId(BAD_LOGIN_USER.username);
    }
    if (!id) throw new Error(`${BAD_LOGIN_USER.username} still not found after create`);

    // Clear any required action an earlier run left pending, for the same
    // reason: a fixture that cannot complete a login proves nothing.
    await ctx.put(`${ADMIN}/users/${id}`, {
      data: {
        email: 'e2e-badlogin@example.invalid',
        emailVerified: true,
        firstName: 'E2E',
        lastName: 'BadLogin',
        requiredActions: [],
      },
    });

    const pw = await ctx.put(`${ADMIN}/users/${id}/reset-password`, {
      data: { type: 'password', value: BAD_LOGIN_USER.password, temporary: false },
    });
    if (!pw.ok()) {
      throw new Error(`could not set the password: ${pw.status()} ${(await pw.text()).slice(0, 200)}`);
    }
  } finally {
    await ctx.dispose();
  }
}

/**
 * Clear Keycloak's attack-detection state for one user.
 *
 * THE ALTERNATIVE IS A SLEEP, and it would be a minute long. Clearing the lock
 * is what lets a bad-password journey run back-to-back with the next test
 * without either waiting out the quick-login window or pretending the window
 * does not exist.
 */
export async function clearLoginFailures(username: string): Promise<void> {
  const id = await findUserId(username);
  if (!id) return;
  const ctx = await adminCtx();
  try {
    await ctx.delete(`${ADMIN}/attack-detection/brute-force/users/${id}`);
  } finally {
    await ctx.dispose();
  }
}

/** How Keycloak currently sees an account — for diagnosing a lockout. */
export async function bruteForceStatus(username: string): Promise<any> {
  const id = await findUserId(username);
  if (!id) return null;
  const ctx = await adminCtx();
  try {
    const resp = await ctx.get(`${ADMIN}/attack-detection/brute-force/users/${id}`);
    return resp.ok() ? resp.json() : null;
  } finally {
    await ctx.dispose();
  }
}
