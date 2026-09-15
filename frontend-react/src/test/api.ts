/**
 * api.ts — the frontend's API mocking strategy.
 *
 * WHY NOT MSW. Mock Service Worker is the usual answer and it is a good tool:
 * it intercepts at the network layer, so the code under test is unaware it is
 * being mocked. Two things argue against it here.
 *
 * First, everything this SPA does goes through `lib/api.ts`, which is a thin
 * wrapper over one global `fetch`. There is no second transport, no websocket
 * client, no XHR left over from an older generation — so the seam MSW buys is
 * a seam that already exists and has exactly one shape.
 *
 * Second, this is an air-gapped product whose build rule is stated at the top
 * of vite.config.ts: everything bundles locally, no runtime CDN imports. Every
 * dependency added here has to be vendored, licence-audited and listed in
 * THIRD-PARTY-NOTICES.md. MSW brings a service-worker runtime and an interceptor
 * stack to replace roughly forty lines. That trade is worth making when a
 * project's I/O is varied; it is not worth making for one `fetch`.
 *
 * If a future surface starts talking over something other than `fetch` — a
 * websocket for live events is the obvious candidate — revisit this. The
 * helper's shape (a route table, assertions on what was called) is deliberately
 * the same shape MSW would give, so the swap would be mechanical.
 *
 * USAGE
 *
 *   const api = mockApi({
 *     'GET /api/auth/config': { mode: 'dev' },
 *     'GET /api/me': { kind: 'user', subject: 'ana', roles: ['viewer'],
 *                      permissions: { camera_view: false } },
 *     'PUT /api/me/password': { status: 403, body: { detail: 'Wrong password' } },
 *   });
 *   ...
 *   expect(api.called('PUT /api/me/password')).toBe(true);
 *   expect(api.bodyOf('PUT /api/me/password')).toEqual({ ... });
 */
import { vi } from 'vitest';

/** What a route may answer with: a plain body, or a full response spec. */
export type RouteReply =
  | unknown
  | {
      status?: number;
      body?: unknown;
      /** Response headers, for the few callers that read one. */
      headers?: Record<string, string>;
    };

export interface MockApi {
  /** The vi.fn() installed on globalThis.fetch, for direct assertions. */
  fetch: ReturnType<typeof vi.fn>;
  /** Every call made, in order, as "METHOD url". */
  calls: string[];
  /** Was this route called? Key is "METHOD /path", same as the route table. */
  called: (key: string) => boolean;
  /** How many times. */
  callCount: (key: string) => number;
  /** The parsed JSON request body of the first call to this route. */
  bodyOf: (key: string) => any;
  /** Replace or add a route mid-test (e.g. to make a retry succeed). */
  set: (key: string, reply: RouteReply) => void;
}

function isResponseSpec(reply: RouteReply): reply is { status?: number; body?: unknown; headers?: Record<string, string> } {
  return (
    !!reply &&
    typeof reply === 'object' &&
    !Array.isArray(reply) &&
    ('status' in (reply as object) || 'body' in (reply as object) || 'headers' in (reply as object))
  );
}

/**
 * Install a fetch stub answering the given routes.
 *
 * Keys are `"<METHOD> <path>"`. A request to a path with no matching route
 * throws, naming the path — the same fail-closed rule as setup.ts, because a
 * component quietly fetching something the test did not anticipate is a fact
 * about the component the test should be forced to state.
 */
export function mockApi(routes: Record<string, RouteReply> = {}): MockApi {
  const table = new Map<string, RouteReply>(Object.entries(routes));
  const calls: string[] = [];
  const bodies = new Map<string, any[]>();

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = (init?.method || 'GET').toUpperCase();
    const key = `${method} ${url}`;
    calls.push(key);

    if (init?.body) {
      let parsed: any = init.body;
      try { parsed = JSON.parse(String(init.body)); } catch { /* not JSON */ }
      bodies.set(key, [...(bodies.get(key) || []), parsed]);
    }

    if (!table.has(key)) {
      throw new Error(
        `Unmocked API call: ${key}\n` +
        `Known routes: ${[...table.keys()].join(', ') || '(none)'}\n` +
        `Add it to mockApi({...}) if the component is supposed to make it.`,
      );
    }

    const reply = table.get(key)!;
    const spec = isResponseSpec(reply) ? reply : { status: 200, body: reply };
    const status = spec.status ?? 200;
    const body = 'body' in spec ? spec.body : undefined;

    return new Response(
      status === 204 || body === undefined ? null : JSON.stringify(body),
      {
        status,
        headers: { 'Content-Type': 'application/json', ...(spec.headers || {}) },
      },
    );
  });

  globalThis.fetch = fetchMock as unknown as typeof fetch;

  return {
    fetch: fetchMock,
    calls,
    called: (key) => calls.includes(key),
    callCount: (key) => calls.filter((c) => c === key).length,
    bodyOf: (key) => (bodies.get(key) || [])[0],
    set: (key, reply) => { table.set(key, reply); },
  };
}

/** A `Me` for tests, with only the fields a test cares about spelled out. */
export function principal(over: Partial<{
  kind: 'user' | 'service' | 'dev';
  subject: string;
  roles: string[];
  permissions: Record<string, boolean>;
}> = {}) {
  return {
    kind: 'user' as const,
    subject: 'tester',
    roles: ['viewer'],
    permissions: {},
    ...over,
  };
}
