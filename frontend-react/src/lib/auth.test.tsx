/**
 * AuthProvider — the three states every authenticated surface hangs off.
 *
 * This is the component the whole SPA waits on: nothing routes until it
 * settles, so its loading, error and success states are not cosmetic — they are
 * the difference between "signing you in", "you are locked out and here is
 * why", and the application. None of the three had a test, and the error branch
 * in particular is invisible in development, where authentication succeeds.
 *
 * TWO THINGS THIS FILE HAS TO DO CAREFULLY.
 *
 * `kc` is module-level state in auth.tsx — one Keycloak client for the tab.
 * That is right for an application and poison for a test file: a test that
 * takes the OIDC branch leaves a client behind, and the NEXT test's
 * `authHeaders()` then tries to refresh a token that was never issued and
 * throws "Session expired". So every test re-imports the module fresh rather
 * than sharing one. Finding this was the first thing these tests did.
 *
 * keycloak-js is mocked at the module boundary. Not to avoid the OIDC path —
 * the point is to be able to REACH it and assert on the branch decision —
 * but because the real client performs a full-page redirect, which jsdom
 * cannot do and which would tell us nothing about this component anyway.
 */
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mockApi, principal } from '@/test/api';

/** Records every Keycloak construction so a test can assert which branch ran. */
const keycloakConstructed: any[] = [];

vi.mock('keycloak-js', () => ({
  default: class FakeKeycloak {
    token = 'fake-token';
    constructor(cfg: any) { keycloakConstructed.push(cfg); }
    async init() { return true; }
    async updateToken() { return true; }
    login() {}
    logout() {}
  },
}));

beforeEach(() => { keycloakConstructed.length = 0; });

/**
 * Mount a fresh AuthProvider. Fresh matters: see the note above about `kc`.
 * The probe is built here so it closes over the same module instance the
 * provider came from — a `useAuth` imported statically would read a different
 * context object and always see the default value.
 */
async function renderAuth() {
  vi.resetModules();
  const { AuthProvider, useAuth } = await import('./auth');

  function Probe() {
    const { me, isAdmin, ready } = useAuth();
    return (
      <div>
        <span data-testid="ready">{String(ready)}</span>
        <span data-testid="subject">{me?.subject ?? '-'}</span>
        <span data-testid="roles">{(me?.roles ?? []).join(',')}</span>
        <span data-testid="is-admin">{String(isAdmin)}</span>
      </div>
    );
  }

  return render(<AuthProvider><Probe /></AuthProvider>);
}

const DEV_MODE = {
  'GET /api/auth/config': { mode: 'dev' },
  'GET /api/me': principal(),
};

describe('AuthProvider — loading', () => {
  it('shows that it is authenticating before anything resolves', async () => {
    mockApi(DEV_MODE);
    await renderAuth();
    expect(screen.getByText('Authenticating…')).toBeInTheDocument();
  });

  it('does not mount the application while it is still deciding', async () => {
    mockApi(DEV_MODE);
    await renderAuth();
    // The gate. A page that mounts before the principal is known makes its own
    // API calls with no identity behind them.
    expect(screen.queryByTestId('ready')).not.toBeInTheDocument();
  });
});

describe('AuthProvider — success', () => {
  it('renders the application once the principal is known', async () => {
    mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': principal({ subject: 'ana', roles: ['operator'] }),
    });
    await renderAuth();

    expect(await screen.findByTestId('subject')).toHaveTextContent('ana');
    expect(screen.getByTestId('roles')).toHaveTextContent('operator');
    expect(screen.queryByText('Authenticating…')).not.toBeInTheDocument();
  });

  it('asks the backend who the caller is', async () => {
    // /me is authoritative: it carries the EFFECTIVE capability map, which the
    // token does not. A frontend deriving permissions from the JWT would
    // ignore every grant made under Roles & permissions.
    const api = mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': principal({ subject: 'ana' }),
    });
    await renderAuth();
    await screen.findByTestId('subject');
    expect(api.called('GET /api/me')).toBe(true);
  });

  it('builds no Keycloak client in dev mode', async () => {
    mockApi(DEV_MODE);
    await renderAuth();
    await screen.findByTestId('subject');
    expect(keycloakConstructed).toHaveLength(0);
  });
});

describe('AuthProvider — error', () => {
  it('shows why the caller is locked out instead of spinning forever', async () => {
    // A transport failure — the API unreachable, which is what an operator
    // hits when the stack is half up.
    mockApi({ 'GET /api/auth/config': { mode: 'dev' } });
    // /me deliberately unrouted: mockApi throws, exactly as a failed call
    // reaches the provider.
    await renderAuth();

    expect(await screen.findByText(/Unmocked API call/)).toBeInTheDocument();
    expect(screen.queryByText('Authenticating…')).not.toBeInTheDocument();
  });

  it('never mounts the application when authentication failed', async () => {
    mockApi({ 'GET /api/auth/config': { mode: 'dev' } });
    await renderAuth();

    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByTestId('ready')).not.toBeInTheDocument();
    expect(screen.queryByTestId('subject')).not.toBeInTheDocument();
  });

  it('never mounts the app as administrator when /me fails', async () => {
    // The regression this guards is worth stating plainly, because it was live
    // until 2026-09-10 and left no trace: `fetch` does not reject on a 5xx, so
    // `.then(r => r.json())` parsed the ERROR BODY as the principal. `me`
    // became `{detail: "..."}`, `me.kind` was undefined, and
    // `isAdmin: me.kind !== 'user'` evaluated TRUE — the shell mounted with
    // every admin-gated nav item visible to a caller whose identity had not
    // been established.
    //
    // Both halves are asserted: it must not be ready, and it must not be
    // admin. Checking only `ready` would still pass if the flag were computed
    // before the guard.
    mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': { status: 500, body: { detail: 'boom' } },
    });
    await renderAuth();

    expect(await screen.findByText('boom')).toBeInTheDocument();
    expect(screen.queryByTestId('ready')).not.toBeInTheDocument();
    expect(screen.queryByTestId('is-admin')).not.toBeInTheDocument();
  });

  it('surfaces the status when a failed /me carries no detail', async () => {
    mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': { status: 502, body: {} },
    });
    await renderAuth();

    expect(await screen.findByText(/HTTP 502/)).toBeInTheDocument();
    expect(screen.queryByTestId('ready')).not.toBeInTheDocument();
  });

  it('does not start a default OIDC login when the config fetch fails', async () => {
    // The other half of the same missing `r.ok`. A 5xx whose body is JSON used
    // to parse into a config with no `mode`, fall past the dev check, and
    // begin a real login against the DEFAULT realm and client id — a redirect
    // to a Keycloak that may not be the one this deployment uses, triggered by
    // the API merely being unhealthy.
    mockApi({
      'GET /api/auth/config': { status: 503, body: { detail: 'API unavailable' } },
      'GET /api/me': principal(),
    });
    await renderAuth();

    expect(await screen.findByText('API unavailable')).toBeInTheDocument();
    expect(keycloakConstructed).toHaveLength(0);
    expect(screen.queryByTestId('ready')).not.toBeInTheDocument();
  });
});

describe('AuthProvider — who counts as an administrator', () => {
  it('treats a user holding the admin role as an administrator', async () => {
    mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': principal({ kind: 'user', roles: ['admin'] }),
    });
    await renderAuth();
    expect(await screen.findByTestId('is-admin')).toHaveTextContent('true');
  });

  it('does not promote a user who merely holds other roles', async () => {
    mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': principal({ kind: 'user', roles: ['supervisor', 'dpo'] }),
    });
    await renderAuth();
    expect(await screen.findByTestId('is-admin')).toHaveTextContent('false');
  });

  it('treats a non-user principal as an administrator', async () => {
    // `me.kind !== 'user'` — the dev bypass and the internal service key are
    // trusted for everything by the API (Principal.has_any), so the UI must
    // agree or it hides surfaces the caller can actually use.
    mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': principal({ kind: 'dev', roles: [] }),
    });
    await renderAuth();
    expect(await screen.findByTestId('is-admin')).toHaveTextContent('true');
  });

  it('survives a principal carrying no roles array', async () => {
    // Defensive: `(me.roles || [])`. An older /me, or a service principal, may
    // omit it — and a crash here takes the application down before it renders.
    mockApi({
      'GET /api/auth/config': { mode: 'dev' },
      'GET /api/me': { kind: 'user', subject: 'ana', permissions: {} },
    });
    await renderAuth();
    expect(await screen.findByTestId('is-admin')).toHaveTextContent('false');
  });
});
