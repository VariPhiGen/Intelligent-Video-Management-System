/**
 * playwright.config.ts — how the journeys run.
 *
 * DIAGNOSTICS ARE THE POINT OF THE SETTINGS BELOW. An E2E failure that a
 * developer cannot explain from the artefacts is a test that will be deleted,
 * so a failing run leaves a trace (every action, every network call, the DOM at
 * each step), a video, and a screenshot. On a passing run it leaves nothing,
 * because the cost only makes sense against a failure.
 *
 * NO RETRIES, ANYWHERE — including CI, where the reflex is strongest. This
 * product's intermittent failures are more likely to be real: a reconnect race,
 * a relay path that was not torn down, a token refreshed a beat late. A retry
 * turns exactly those into a green tick. The strategy's third discipline rule
 * says a flake is fixed or deleted within a week, and `retries: 0` is what
 * makes that rule enforceable rather than aspirational.
 *
 * FULLY SEQUENTIAL. `workers: 1` is not a performance oversight. The journeys
 * share one appliance: a camera onboarded by one test is visible to every
 * other, MediaMTX paths are global, and the scan lock is deliberately exclusive
 * across the whole stack. Parallel workers would either fight over that state
 * or need it partitioned, and partitioning would mean testing something other
 * than the product.
 */
import { defineConfig, devices } from '@playwright/test';

const API_PORT = process.env.E2E_API_PORT ?? '19091';
const BASE_URL = process.env.E2E_BASE_URL ?? `http://localhost:${API_PORT}`;

export default defineConfig({
  testDir: './tests',
  // Journeys drive a real stack: onboarding waits on an ONVIF probe and an
  // ffprobe, both of which are seconds rather than milliseconds.
  timeout: 120_000,
  expect: { timeout: 20_000 },

  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: !!process.env.CI,

  reporter: [
    ['list'],
    ['html', { outputFolder: 'playwright-report', open: 'never' }],
  ],

  use: {
    baseURL: BASE_URL,
    // Keycloak is a full-page redirect to a DIFFERENT origin, so nothing here
    // may assume same-origin navigation.
    ignoreHTTPSErrors: true,
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 20_000,
    navigationTimeout: 40_000,
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // A fixed viewport keeps the video wall's tile geometry deterministic;
        // its cell-crop behaviour is a function of the container's aspect.
        viewport: { width: 1440, height: 900 },
      },
    },
  ],

  // NO webServer BLOCK. Playwright's `webServer` is for a dev server it can
  // start and stop; this suite needs fourteen containers, migrations applied,
  // a realm imported and two cameras serving RTSP. That readiness is scripted
  // in e2e/scripts/up.sh, which reports what it is waiting for and dumps the
  // stack log on a timeout — far better diagnostics than a port poll. The
  // global setup below fails fast with instructions if the stack is not up.
  globalSetup: './support/global-setup.ts',
});
