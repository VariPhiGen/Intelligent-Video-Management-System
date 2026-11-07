/**
 * branding.ts — the installation's client logos (light + dark theme), the Video Wall title and
 * the admin's default wall settings (/api/branding, admin-set).
 *
 * Logos are fetched with the Bearer token as Blobs (an <img src> can't carry it) and exposed as
 * object URLs, revoked when they change or the component unmounts. With only one logo uploaded it
 * is used for both themes. Re-polled every minute so an admin's change to the default wall reaches
 * screens that are left open (a wall TV is never reloaded); the logo blobs are only re-fetched when
 * their version changes.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiBlob, apiFetch } from './api';

export interface Branding {
  wall_title: string | null;
  has_logo: boolean;
  has_logo_dark: boolean;
  logo_version: string | null;
  logo_dark_version: string | null;
  wall_defaults: Record<string, unknown> | null;
}

export const DEFAULT_WALL_TITLE = 'Video Wall';
const POLL_MS = 60_000;

/** The app theme, live (Shell sets html[data-theme]). */
export function useDocTheme(): 'light' | 'dark' {
  const read = () => (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');
  const [theme, setTheme] = useState<'light' | 'dark'>(read);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(read()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);
  return theme;
}

function useLogoBlob(theme: 'light' | 'dark', has: boolean, version: string | null): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const current = useRef<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const release = () => { if (current.current) { URL.revokeObjectURL(current.current); current.current = null; } };
    if (!has) { release(); setUrl(null); return; }
    apiBlob(`/branding/logo?theme=${theme}&v=${encodeURIComponent(version || '')}`)
      .then(blob => {
        if (cancelled) return;
        release();
        current.current = URL.createObjectURL(blob);
        setUrl(current.current);
      })
      .catch(() => { if (!cancelled) setUrl(null); });
    return () => { cancelled = true; };
  }, [theme, has, version]);
  useEffect(() => () => { if (current.current) URL.revokeObjectURL(current.current); }, []);
  return url;
}

export function useBranding(): {
  branding: Branding; loaded: boolean; logoLightUrl: string | null; logoDarkUrl: string | null; refresh: () => void;
} {
  const [branding, setBranding] = useState<Branding>({
    wall_title: null, has_logo: false, has_logo_dark: false, logo_version: null, logo_dark_version: null,
    wall_defaults: null,
  });
  // false until the first answer (or error): the wall waits for the admin default rather than
  // briefly starting streams for the built-in "all cameras" default and then dropping them.
  const [loaded, setLoaded] = useState(false);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick(t => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    apiFetch<Branding>('/branding')
      .then(b => { if (!cancelled) setBranding(b); })
      .catch(() => { /* keep last */ })
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [tick]);
  useEffect(() => {
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const logoLightUrl = useLogoBlob('light', branding.has_logo, branding.logo_version);
  const logoDarkUrl = useLogoBlob('dark', branding.has_logo_dark, branding.logo_dark_version);
  return { branding, loaded, logoLightUrl, logoDarkUrl, refresh };
}

/** The logo for a theme, falling back to the other one when only one is uploaded. */
export const logoFor = (theme: 'light' | 'dark', light: string | null, dark: string | null) =>
  (theme === 'dark' ? dark || light : light || dark);
