/**
 * Shell navigation gating — the authorisation behaviour a user actually sees.
 *
 * The API decides what a caller may DO; this decides what they are shown. The
 * two are separate mechanisms and only the first has tests, which is backwards
 * for the one that runs on every page load. A nav item wrongly shown is a
 * support call and a dead-end 403; a nav item wrongly hidden is a feature the
 * customer paid for and cannot find.
 *
 * The rule has a deliberate ASYMMETRY, stated in Shell.tsx's own comment, and
 * it is the thing these tests exist to hold still:
 *
 *   ordinary items   granted UNLESS the map explicitly says false, so a
 *                    principal whose /me predates a new capability still sees
 *                    the item rather than silently losing it on deploy.
 *   Administration   granted only on an explicit `=== true`, because it opens
 *                    an admin surface and "not mentioned" must never mean yes.
 *
 * Written the direction round that catches the dangerous edit: the admin tests
 * assert that absence and undefined and a truthy-but-not-true value all keep
 * the item HIDDEN. Relaxing that check to `!== false`, or to a plain truthiness
 * test, has to turn this file red.
 */
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

let currentMe: any = null;
let currentIsAdmin = false;

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    me: currentMe, isAdmin: currentIsAdmin,
    ready: true, error: null, logout: () => {},
  }),
  authHeaders: async () => ({}),
}));

// The shell polls host and NVR health on an interval. Neither has anything to
// do with nav gating; stubbed so the tests are about one thing.
vi.mock('@/lib/api', () => ({
  apiFetch: async () => ({ disk: { used_pct: 10 } }),
  ApiError: class extends Error {},
}));

import { Shell } from './Shell';

function renderShell(me: any, isAdmin = false) {
  currentMe = me;
  currentIsAdmin = isAdmin;
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({
      connected_streams: 0, enabled_cameras: 0, total_cameras: 0, host: {},
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
  ) as unknown as typeof fetch;

  return render(
    <MemoryRouter initialEntries={['/live']}>
      <Shell />
    </MemoryRouter>,
  );
}

const principal = (permissions: Record<string, boolean>, over: any = {}) => ({
  kind: 'user', subject: 'ana', roles: ['viewer'], permissions, ...over,
});

/** Nav entries are links; a section header is not. Query the rail, not the page. */
const navItem = (label: string) => screen.queryByRole('link', { name: new RegExp(label) });

beforeEach(() => { currentMe = null; currentIsAdmin = false; });

describe('Shell nav — ordinary capabilities are granted unless denied', () => {
  it('shows an item a principal explicitly holds', () => {
    renderShell(principal({ live_view: true }));
    expect(navItem('Live View')).toBeInTheDocument();
  });

  it('hides an item the map explicitly denies', () => {
    renderShell(principal({ smart_search: false }));
    expect(navItem('Smart Search')).not.toBeInTheDocument();
  });

  it('shows an item the map does not mention at all', () => {
    // The forward-compatibility rule: a capability added after this user's
    // session began is absent from their /me, and must not disappear from
    // their sidebar until an administrator actually denies it.
    renderShell(principal({ live_view: true }));
    expect(navItem('Map')).toBeInTheDocument();
  });

  it('shows everything to a principal with an empty permission map', () => {
    renderShell(principal({}));
    for (const label of ['Live View', 'Smart Search', 'Playback & Cases', 'Map', 'Cameras']) {
      expect(navItem(label)).toBeInTheDocument();
    }
  });

  it('denies each capability independently', () => {
    renderShell(principal({ smart_search: false, camera_view: false, live_view: true }));
    expect(navItem('Smart Search')).not.toBeInTheDocument();
    expect(navItem('Cameras')).not.toBeInTheDocument();
    expect(navItem('Live View')).toBeInTheDocument();
    expect(navItem('Map')).toBeInTheDocument();
  });
});

describe('Shell nav — Administration needs an explicit yes', () => {
  it('shows Administration to an admin', () => {
    renderShell(principal({}, { roles: ['admin'] }), true);
    expect(navItem('Administration')).toBeInTheDocument();
  });

  it('hides Administration from a non-admin with no admin-access capability', () => {
    renderShell(principal({ live_view: true }), false);
    expect(navItem('Administration')).not.toBeInTheDocument();
  });

  it('hides Administration when the capability is merely absent', () => {
    // The asymmetry. For an ordinary item, absent means shown. For this one,
    // absent must mean hidden — `perms[c] === true`, not `!== false`.
    renderShell(principal({}), false);
    expect(navItem('Administration')).not.toBeInTheDocument();
  });

  it('hides Administration when the map has no entries at all', () => {
    renderShell(principal({}), false);
    expect(navItem('Administration')).not.toBeInTheDocument();
  });

  it('hides Administration from a principal with every OTHER capability granted', () => {
    // The strongest form: a supervisor holding the full operational set is
    // still not an administrator.
    renderShell(principal({
      live_view: true, smart_search: true, playback_search: true,
      map_view: true, camera_view: true, camera_manage: true,
      ai_analytics: true, export_reports: true,
    }), false);
    expect(navItem('Administration')).not.toBeInTheDocument();
  });

  it('survives a null principal without showing Administration', () => {
    // `me` is null for a moment on every load. Whatever else the rail does
    // then, it must not offer the admin surface.
    renderShell(null, false);
    expect(navItem('Administration')).not.toBeInTheDocument();
  });
});

// Anything asserting on what an EXTENSION contributes lives with that
// extension — src/extensions/compliance/Shell.nav.test.tsx. Written here, it
// ran against a build where the extension had been stripped and either failed
// (asserting a nav entry that could not exist) or, worse, passed while
// asserting the absence of something absent by construction. Tests move with
// the code they cover; the section-header MECHANISM is core, so it stays, and
// is written below against Administration rather than against an extension.
describe('Shell nav — section headers follow their items', () => {
  it('keeps a section header while anything under it is still visible', () => {
    // Administration is core and admin-gated, so an admin always has exactly
    // one item under Govern in every build — which is what makes this a test
    // of the heading rule rather than of whatever happens to be installed.
    renderShell(principal({ live_view: true }), true);
    expect(screen.getByText('Operate')).toBeInTheDocument();
    expect(screen.getByText('Govern')).toBeInTheDocument();
    expect(navItem('Administration')).toBeInTheDocument();
  });

  it('drops a section header once every item under it is hidden', () => {
    // Same principal, no admin: Administration goes, and with nothing else
    // core under Govern the heading must go too. An empty heading advertises
    // a surface the principal cannot reach.
    renderShell(principal({ live_view: true }), false);
    expect(screen.getByText('Operate')).toBeInTheDocument();
    expect(navItem('Administration')).not.toBeInTheDocument();
  });
});
