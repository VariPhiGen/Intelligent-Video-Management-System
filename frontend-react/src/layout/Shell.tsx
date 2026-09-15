/**
 * Shell.tsx — rail (OPERATE / MANAGE / GOVERN) + command bar (title, site, the
 * appliance's live readings, theme/language) around the routed page.
 * Nav aliases keep Cameras highlighted on its sub-pages.
 */
import { useEffect, useState, type ReactNode } from 'react';
import { LICENSE_NAME, LICENSE_URL, SOURCE_URL, SOURCE_CONFIGURED } from '../lib/legal';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { adminAccessCaps, extensionNav } from '@/extensions';
import { ChangePasswordModal } from '@/components/ChangePasswordModal';
import { BRAND, Logo, SiteChip } from '@/components/Logo';
import { Icon, navIcon } from '@/components/Icon';

// Descending privilege — mirrors ROLE_PRIORITY in the api's
// extensions/identity/users.py, so the badge shows the same primary role the
// Users table does in builds that have one.
const ROLE_PRIORITY: [string, string][] = [
  ['admin', 'Administrator'],
  ['supervisor', 'Supervisor'],
  ['dpo', 'Data protection officer'],
  ['operator', 'Operator'],
  ['viewer', 'Viewer'],
];

const TITLES: Record<string, string> = {
  '/live': 'Live View', '/smartsearch': 'Smart Search', '/playback': 'Playback & Cases',
  '/map': 'Map', '/peripherals': 'Peripherals', '/cameras': 'Camera Inventory',
  '/cameras/add': 'Add cameras', '/cameras/config': 'Configuration',
  '/ai': 'AI Analytics', '/admin': 'Administration',
  // Extension pages title themselves from the nav entry they registered — the
  // header must not carry a route the build may not contain.
  ...Object.fromEntries(extensionNav.map(n => [n.to, n.label])),
};

function useClock(): string {
  const [now, setNow] = useState('');
  useEffect(() => {
    const t = setInterval(() =>
      setNow(new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })), 1000);
    return () => clearInterval(t);
  }, []);
  return now;
}

/** Segmented pill toggle (language). */
function Seg({ value, options, onChange, title }: {
  value: string;
  options: [string, ReactNode][];
  onChange: (v: string) => void;
  title?: string;
}) {
  return (
    <div className="topseg" title={title}>
      {options.map(([v, label]) => (
        <button key={v} className={`topseg-item${value === v ? ' active' : ''}`} onClick={() => onChange(v)}>
          {label}
        </button>
      ))}
    </div>
  );
}

/**
 * A live usage metric (GPU, storage) — coloured by level, with a hairline bar so
 * the reading is legible at a glance rather than only after parsing the number.
 *
 * A null reading is NOT neutral here: if the hardware exists and we still
 * can't read it, that is a fault worth surfacing, so "—" goes amber with an
 * explanation. Hardware that simply isn't fitted is handled by not rendering
 * the metric at all — a gauge permanently stuck on "—" teaches people to
 * ignore the whole bar.
 */
function Metric({ label, pct, hint, faulted }: {
  label: string; pct: number | null; hint: string; faulted?: boolean;
}) {
  const color = pct == null ? (faulted ? 'var(--yellow)' : 'var(--dim)')
    : pct >= 90 ? 'var(--red)' : pct >= 75 ? 'var(--yellow)' : 'var(--green)';
  return (
    <span className="topmetric" title={hint}>
      <span className="tm-label">{label}</span>
      <span className="meter-track">
        <span className="meter-fill" style={{ width: `${Math.min(100, pct ?? 0)}%`, background: color }} />
      </span>
      <span className="tm-val" style={pct == null || pct >= 75 ? { color } : undefined}>
        {pct == null ? '—' : `${Math.round(pct)}%`}
      </span>
    </span>
  );
}

// Past this many cameras one tick each stops being countable at a glance, so
// the strip becomes proportional: the same width, each tick a share of the fleet.
const MAX_TICKS = 24;

/**
 * The fleet strip: the one reading an operator checks on every page. One tick
 * per enabled camera, teal while it streams and red when it does not, so "one
 * camera is down" is a red gap in the strip before it is a number to read.
 */
function Fleet({ health }: { health: { live: number; enabled: number } | 'down' | null }) {
  if (health === 'down') {
    return <span className="topmetric fleet fleet-down" title="The core service did not answer its health check">Core service unreachable</span>;
  }
  if (health === null) {
    return <span className="topmetric fleet"><span className="skel" style={{ width: 64, height: 10 }} /></span>;
  }
  const { live, enabled } = health;
  if (!enabled) {
    return <span className="topmetric fleet" title="No cameras are enabled">No cameras enabled</span>;
  }
  const n = Math.min(enabled, MAX_TICKS);
  const lit = enabled <= MAX_TICKS ? live : Math.round((live / enabled) * MAX_TICKS);
  const down = enabled - live;
  return (
    <span className="topmetric fleet"
          title={down === 0 ? 'All enabled cameras streaming' : `${down} of ${enabled} enabled cameras not streaming`}>
      <span className="fleet-ticks" aria-hidden="true">
        {Array.from({ length: n }, (_, i) => <i key={i} className={i < lit ? '' : 'off'} />)}
      </span>
      <span className="tm-val">{live}<span style={{ color: 'var(--dim)' }}>/{enabled}</span></span>
      <span className="tm-label">live</span>
    </span>
  );
}

// `cap` is the capability that reveals the item (from the Roles & permissions
// matrix). Admin holds every capability, so admin sees everything. `/admin`
// gates on the admin role directly, not a capability.
const NAV = [
  { section: 'Operate' },
  { to: '/live', glyph: '▦', label: 'Live View', cap: 'live_view' },
  { to: '/smartsearch', glyph: '✦', label: 'Smart Search', cap: 'smart_search' },
  { to: '/playback', glyph: '⏱', label: 'Playback & Cases', cap: 'playback_search' },
  { to: '/map', glyph: '⬡', label: 'Map', cap: 'map_view' },
  { section: 'Manage' },
  // { to: '/peripherals', glyph: '⚡', label: 'Peripherals', cap: 'peripherals' },
  { to: '/cameras', glyph: '▤', label: 'Cameras', counted: true, cap: 'camera_view' },
  { to: '/ai', glyph: '▣', label: 'AI Analytics', cap: 'ai_analytics' },
  { section: 'Govern' },
  { to: '/admin', glyph: '⚷', label: 'Administration', adminOnly: true },
] as const;

// Extensions append their own entries under a named section. Built once at
// module load: the glob is resolved at build time, so this cannot change at
// runtime and there is nothing to memoise.
const NAV_WITH_EXTENSIONS: readonly any[] = (() => {
  const out: any[] = [...NAV];
  for (const item of extensionNav) {
    // Insert after the last item belonging to the named section, so the entry
    // lands inside that group rather than at the end of the rail.
    const { underSection, ...entry } = item;   // strip it: see ExtensionNavItem
    const head = out.findIndex(i => 'section' in i && i.section === underSection);
    if (head < 0) { out.push(entry); continue; }
    let end = head + 1;
    while (end < out.length && !('section' in out[end])) end++;
    out.splice(end, 0, entry);
  }
  return out;
})();

export function Shell() {
  const { me, isAdmin, logout } = useAuth();
  const loc = useLocation();
  const clock = useClock();
  const [theme, setTheme] = useState(() => document.documentElement.dataset.theme || 'light');
  const [lang, setLang] = useState(() => { try { return localStorage.getItem('vms-lang') || 'en'; } catch { return 'en'; } });
  const [camCount, setCamCount] = useState<number | null>(null);
  // null = still loading; 'down' = /health unreachable. Kept structured (not a
  // prerendered string) so the fleet strip can colour and count its ticks.
  const [health, setHealth] = useState<{ live: number; enabled: number } | 'down' | null>(null);
  // present=false → no GPU fitted (hide the gauge); present=true with pct=null
  // → a GPU we cannot read, which is a fault, not an absence.
  const [gpu, setGpu] = useState<{ present: boolean; pct: number | null }>({ present: false, pct: null });
  const [stor, setStor] = useState<number | null>(null);
  const [pwOpen, setPwOpen] = useState(false);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem('vms-theme', theme); } catch { /* private mode */ }
    // Mirror to a cookie so the Keycloak LOGIN page can pick the same theme up.
    // It runs on a different port, so it's a different origin and can't read our
    // localStorage — but cookie scope is host-based and ignores the port, which
    // makes this the one channel that crosses. deploy/keycloak/themes/vms
    // reads it back. (Only carries across if Keycloak is on the same hostname —
    // a fully separate OIDC host falls back to the OS preference.)
    try {
      const secure = location.protocol === 'https:' ? '; Secure' : '';
      document.cookie = `vms-theme=${theme}; path=/; max-age=31536000; SameSite=Lax${secure}`;
    } catch { /* cookies blocked — login just follows the OS instead */ }
  }, [theme]);

  // Language choice: persist locally and share with the Keycloak login via its
  // KEYCLOAK_LOCALE cookie, so the sign-in page follows the same language.
  // (The dashboard text itself is not translated yet — no SPA i18n.)
  useEffect(() => {
    document.documentElement.lang = lang;
    try { localStorage.setItem('vms-lang', lang); } catch { /* private mode */ }
    try {
      const secure = location.protocol === 'https:' ? '; Secure' : '';
      document.cookie = `KEYCLOAK_LOCALE=${lang}; path=/; max-age=31536000; SameSite=Lax${secure}`;
    } catch { /* cookies blocked */ }
  }, [lang]);

  // Live GPU + fleet health from /health (host metrics), every 10s.
  useEffect(() => {
    const load = () =>
      fetch('/health').then(r => r.json()).then((h: any) => {
        setHealth({ live: h.connected_streams, enabled: h.enabled_cameras });
        setCamCount(h.total_cameras);
        setGpu({
          present: !!h.host?.gpu?.present,
          pct: h.host?.gpu?.pct ?? h.host?.gpu_pct ?? null,
        });
      }).catch(() => { setHealth('down'); setGpu({ present: false, pct: null }); });
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  // Live storage usage (appliance disk %) from the NVR, every 30s.
  useEffect(() => {
    const load = () =>
      apiFetch('/nvr/health').then((d: any) => setStor(d?.disk?.used_pct ?? null)).catch(() => setStor(null));
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  const roles = me?.roles || [];
  const roleLabel = me?.kind === 'dev' ? 'Developer bypass'
    : ROLE_PRIORITY.find(([r]) => roles.includes(r))?.[1] || roles[0] || 'No role';
  const initials = (me?.subject || '·').replace(/[^a-zA-Z0-9 ]/g, ' ').trim()
    .split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase() || '·';
  const title = TITLES[loc.pathname]
    || Object.entries(TITLES).find(([p]) => loc.pathname.startsWith(p))?.[1] || BRAND.name;
  const nextTheme = theme === 'dark' ? 'light' : 'dark';

  return (
    <div id="app">
      <aside>
        <Logo />
        <nav>
          {(() => {
            // Roles & permissions gating: hide surfaces the principal can't use.
            // A capability is granted unless the map explicitly says false, so a
            // principal whose /me predates a new capability still sees the item.
            const perms: Record<string, boolean> = me?.permissions || {};
            const allowed = NAV_WITH_EXTENSIONS.filter(item => {
              if ('section' in item) return true;
              // A non-admin reaches Administration if an extension contributed
              // a tab gated by a capability they hold. AdminPage hides every
              // other tab for them.
              // Explicit === true (not "granted unless false") because this
              // opens an admin surface.
              if ('adminOnly' in item && item.adminOnly) {
                return isAdmin || adminAccessCaps.some(c => perms[c] === true);
              }
              if ('cap' in item && perms[item.cap] === false) return false;
              return true;
            });
            // Drop a section header that has no visible items under it.
            return allowed.filter((item, i) =>
              !('section' in item) ||
              (allowed[i + 1] && !('section' in allowed[i + 1])));
          })().map((item, i) => {
            if ('section' in item) return <div key={i} className="nav-section">{item.section}</div>;
            const icon = navIcon(item.to);
            return (
              <NavLink key={item.to} to={item.to} title={item.label}
                className={({ isActive }) =>
                  isActive || (item.to === '/cameras' && loc.pathname.startsWith('/cameras')) ? 'active' : ''}>
                <span className="nav-glyph">{icon ? <Icon name={icon} /> : item.glyph}</span>
                <span className="nav-label">{item.label}</span>
                {'counted' in item && item.counted && camCount != null && <span className="nav-count">{camCount}</span>}
              </NavLink>
            );
          })}
        </nav>

        <div className="rail-foot">
          <div className="user-row">
            <div className="avatar" aria-hidden="true">{initials}</div>
            <div className="user-meta">
              <div className="user-name">{me?.kind === 'dev' ? 'Dev bypass' : me?.subject}</div>
              <div className="user-role">{roleLabel}</div>
            </div>
            <button className="iconbtn sm" title="Change my password" onClick={() => setPwOpen(true)}>
              <Icon name="key" size={16} />
            </button>
            <button className="iconbtn sm" title="Sign out" onClick={logout}>
              <Icon name="logout" size={16} />
            </button>
          </div>

          {/* AGPL-3.0 §13: someone using this over a network never sees the
              repository, so the application itself has to say what it is and
              where its source is. Kept deliberately quiet — it is a legal
              notice, not a feature. */}
          <div className="legal-note">
            {BRAND.name} ·{' '}
            <a href={LICENSE_URL} target="_blank" rel="noreferrer noopener">{LICENSE_NAME}</a>
            {SOURCE_CONFIGURED && (
              <> · <a href={SOURCE_URL} target="_blank" rel="noreferrer noopener">Source</a></>
            )}
          </div>
        </div>
      </aside>
      <ChangePasswordModal open={pwOpen} onClose={() => setPwOpen(false)} />
      <div className="workspace">
        {/* Left: where you are. Right: what the appliance is doing right now,
            then how you like the console. */}
        <header className="topbar">
          <span className="topbar-title">{title}</span>
          <SiteChip />
          <div className="topbar-right">
            <div className="status-group">
              <Fleet health={health} />
              {gpu.present && (
                <Metric label="GPU" pct={gpu.pct} faulted={gpu.pct == null}
                  hint={gpu.pct == null
                    ? 'GPU detected but utilization could not be read — check the driver on this appliance'
                    : 'Appliance GPU utilization — busiest device, all workloads'} />
              )}
              <Metric label="Storage" pct={stor} hint="Appliance storage used" />
              <span className="topmetric topbar-clock"><Icon name="clock" size={14} />{clock}</span>
            </div>
            <button className="iconbtn" title={`Switch to ${nextTheme} theme`} onClick={() => setTheme(nextTheme)}>
              <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={17} />
            </button>
            <Seg value={lang} onChange={setLang} title="Interface language (login screen)"
                 options={[['en', 'EN'], ['hi', 'हिं']]} />
          </div>
        </header>
        <main>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
