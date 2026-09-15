/**
 * setup.ts — what every frontend test gets before it runs.
 *
 * Two jobs, and the second is the one that matters.
 *
 * 1. jest-dom matchers (`toBeInTheDocument`, `toHaveTextContent`, …) and React
 *    Testing Library's automatic unmount between tests. Without the unmount,
 *    a component left mounted keeps its timers and its effects running into the
 *    next test, and the failure lands on whichever test happens to be next.
 *
 * 2. `fetch` fails closed. An unmocked call does not quietly return undefined
 *    and surface three assertions later as "cannot read property of
 *    undefined" — it throws immediately, naming the URL nobody stubbed.
 *
 *    This mirrors the guard the API tests grew in P2, for the same reason: a
 *    test that reaches a real service passes on a developer's machine with the
 *    stack up and fails on a runner that has nothing listening. The frontend
 *    version is stricter, because in jsdom there is nothing to reach at all —
 *    an unmocked fetch is always a test that has not said what it expects.
 */
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

beforeEach(() => {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    throw new Error(
      `Unmocked fetch: ${url}\n` +
      `Frontend tests must not reach the network. Stub it with mockApi() ` +
      `from src/test/api.ts, or assert that this call should not happen.`,
    );
  }) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});
