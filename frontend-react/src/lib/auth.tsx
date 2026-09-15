/**
 * auth.tsx — the product's single-login gate, ported from the legacy SPA.
 *
 * Flow: fetch /api/auth/config (public) → dev mode (no Keycloak, api trusts
 * everyone as admin) or OIDC mode (keycloak-js, login-required, PKCE S256,
 * Keycloak URL derived from the browser host unless pinned). The provider
 * renders a splash until authentication settles, then exposes the principal
 * from /me plus a logout handle. Tokens refresh on a 60 s interval.
 */
import Keycloak from 'keycloak-js';
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { Logo } from '@/components/Logo';

export interface Me {
  kind: 'user' | 'service' | 'dev';
  subject: string;
  roles: string[];
  /** Effective capability map from the Roles & permissions matrix. */
  permissions: Record<string, boolean>;
}

interface AuthState {
  ready: boolean;
  error: string | null;
  me: Me | null;
  isAdmin: boolean;
  logout: () => void;
}

let kc: Keycloak | null = null;

/** Bearer header for the current session (empty in dev mode). */
export async function authHeaders(): Promise<Record<string, string>> {
  if (!kc) return {};
  try {
    await kc.updateToken(30);
  } catch {
    kc.login();
    throw new Error('Session expired');
  }
  return kc.token ? { Authorization: `Bearer ${kc.token}` } : {};
}

const AuthCtx = createContext<AuthState>({
  ready: false, error: null, me: null, isAdmin: false, logout: () => {},
});
export const useAuth = () => useContext(AuthCtx);

/**
 * GET some JSON, treating a non-2xx as a failure.
 *
 * `fetch` only rejects on a transport error, so `.then(r => r.json())` parses
 * an error BODY as if it were the payload. Both callers below used to do that,
 * and both consequences were silent:
 *
 *   /api/auth/config — a 5xx whose body is JSON produced a config with no
 *                      `mode`, fell past the dev check, and began a real OIDC
 *                      login against the DEFAULT realm and client id — a
 *                      redirect to a Keycloak that may not be this
 *                      deployment's, triggered by the API merely being
 *                      unhealthy.
 *   /api/me          — a 5xx produced `me = {detail: "..."}`, so `me.kind` was
 *                      undefined and `isAdmin: me.kind !== 'user'` came out
 *                      TRUE. The provider reported ready, the shell mounted,
 *                      and every admin-gated surface appeared for a caller
 *                      whose identity had not been established. The API still
 *                      refused the calls those surfaces made, so it was UI
 *                      exposure rather than privilege escalation — but it is
 *                      exactly what this product forbids: an unknown state
 *                      must render as unknown, never as a plausible value.
 *
 * Failing here puts the provider in its error branch, which is the honest
 * answer: it says why the caller is locked out instead of guessing.
 *
 * Deliberately local rather than reusing api.ts's `toApiError`: api.ts imports
 * `authHeaders` from this module, so depending on it would close an import
 * cycle around the app's authentication.
 */
async function getJson(path: string, init?: RequestInit): Promise<any> {
  const resp = await fetch(path, init);
  if (!resp.ok) {
    let detail = '';
    try {
      const body = await resp.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch { /* no JSON body — fall through to the status message */ }
    throw new Error(detail || `${path} failed (HTTP ${resp.status})`);
  }
  return resp.json();
}

async function initAuth(): Promise<void> {
  const cfg = await getJson('/api/auth/config');
  if (cfg.mode === 'dev') return; // api trusts everyone in dev mode

  // Remember the route across the login redirect. The redirect URI is pinned
  // hash-free (see below), so Keycloak's round-trip drops the #/route — without
  // this, every refresh lands back on the default page. sessionStorage survives
  // the same-tab redirect; skip saving on the callback leg (?code=… present),
  // when the hash has already been stripped.
  const onCallback = /[?&](code|state|session_state)=/.test(location.search);
  if (!onCallback && location.hash && location.hash !== '#/' && location.hash !== '#') {
    try { sessionStorage.setItem('vms-return', location.hash); } catch { /* storage off */ }
  }

  // Turnkey rule: derive Keycloak's URL from the browser host unless pinned.
  const url = cfg.url
    ? cfg.url
    : `${location.protocol}//${location.hostname}:${cfg.port || 8085}`;
  kc = new Keycloak({ url, realm: cfg.realm || 'vms', clientId: cfg.client_id || 'vms-web' });
  const ok = await kc.init({
    onLoad: 'login-required',
    pkceMethod: 'S256',
    checkLoginIframe: false,
    // The app uses hash routing (#/cameras). Keycloak's default 'fragment'
    // response mode would append #state=…&code=… onto that hash and corrupt
    // the callback → token exchange 400s ("Server responded with an invalid
    // status"). Query mode keeps the OAuth params out of the fragment, and
    // the redirect URI is pinned hash-free for the same reason.
    responseMode: 'query',
    redirectUri: location.origin + location.pathname,
  });
  if (!ok) throw new Error('Login required');

  // Put the user back on the page they refreshed on. HashRouter mounts only
  // after auth settles, so it reads this restored hash on first render.
  try {
    const ret = sessionStorage.getItem('vms-return');
    if (ret) {
      sessionStorage.removeItem('vms-return');
      if (location.hash !== ret) location.hash = ret;
    }
  } catch { /* storage off */ }

  setInterval(() => { kc?.updateToken(60).catch(() => kc?.login()); }, 60000);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Omit<AuthState, 'logout'>>({
    ready: false, error: null, me: null, isAdmin: false,
  });

  useEffect(() => {
    (async () => {
      try {
        await initAuth();
        const headers = await authHeaders();
        const me: Me = await getJson('/api/me', { headers });
        setState({
          ready: true, error: null, me,
          isAdmin: me.kind !== 'user' || (me.roles || []).includes('admin'),
        });
      } catch (e: any) {
        setState({ ready: false, error: e.message || 'Authentication failed', me: null, isAdmin: false });
      }
    })();
  }, []);

  const logout = () => {
    try { localStorage.removeItem('vms-theme'); } catch {}
    if (kc) kc.logout({ redirectUri: location.origin });
    else location.reload();
  };

  if (!state.ready) {
    return (
      <div className="overlay open" style={{ background: 'var(--bg)', zIndex: 200 }}>
        <div className="modal" style={{ width: 340, textAlign: 'center', padding: 'var(--s6) var(--s5)' }}>
          {/* Splash: stack the mark over the wordmark and centre the group, so
              the badge sits above the name instead of detaching to the left
              (the sidebar's horizontal row relies on left-aligned text). */}
          <Logo subtitle="Enterprise · Camera Management"
                style={{ border: 'none', padding: 0, margin: '0 auto 20px',
                         flexDirection: 'column', gap: 12, justifyContent: 'center' }} />
          {state.error
            ? <div style={{ color: 'var(--red)', fontSize: 13 }}>{state.error}</div>
            : <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
                <span className="spinner" /><span style={{ color: 'var(--muted)' }}>Authenticating…</span>
              </div>}
        </div>
      </div>
    );
  }
  return <AuthCtx.Provider value={{ ...state, logout }}>{children}</AuthCtx.Provider>;
}
