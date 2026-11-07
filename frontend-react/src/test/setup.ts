/**
 * setup.ts — what every frontend test gets before it runs.
 *
 * Three jobs, and the second is the one that matters.
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
 *
 * 3. A minimal `PointerEvent`. jsdom does not implement it, so Testing
 *    Library's `fireEvent.pointerDown` falls back to a plain Event and silently
 *    drops `clientX`/`clientY`. Any handler that reads coordinates — the video
 *    wall's swipe-to-change-wall is the first — then sees `undefined`, compares
 *    NaN, and does nothing, so the test fails with the gesture looking ignored
 *    when the product is fine. This is a gap in the environment, not a reason
 *    to write the product against mouse events it does not want.
 */
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

if (typeof window !== 'undefined' && !('PointerEvent' in window)) {
  class JsdomPointerEvent extends MouseEvent {
    readonly pointerId: number;
    readonly pointerType: string;
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 1;
      this.pointerType = init.pointerType ?? 'mouse';
    }
  }
  // MouseEvent already carries clientX/clientY, which is the whole point.
  (window as unknown as { PointerEvent: unknown }).PointerEvent = JsdomPointerEvent;
  (globalThis as unknown as { PointerEvent: unknown }).PointerEvent = JsdomPointerEvent;
}

// jsdom ships no ResizeObserver, and any component that measures itself uses
// one. Without this the component throws during commit and the failure reads
// as a broken component rather than a missing browser API.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class NoopResizeObserver implements ResizeObserver {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
    NoopResizeObserver;
  (window as unknown as { ResizeObserver: unknown }).ResizeObserver =
    NoopResizeObserver;
}

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
