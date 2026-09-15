/**
 * AdminPage.tsx — Administration: Audit log | System health | Storage |
 * Activity types, plus whatever tabs optional extensions contribute (in the
 * commercial tiers, Users and Roles & permissions lead the row).
 *
 * A non-admin who holds the capability gating an extension's tab reaches this
 * page too, and sees only that tab — every core tab is admin-only.
 */
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '@/lib/auth';
import { HealthTab } from './HealthTab';
import { StorageTab } from './StorageTab';
import { ActivityTypesTab } from './ActivityTypesTab';
import { AuditTab } from './AuditTab';
import { extensionAdminTabs } from '@/extensions';

type TabKey = string;

const CORE_TABS: [TabKey, string][] = [
  ['audit', 'Audit log'],
  ['health', 'System health'],
  ['storage', 'Storage'],
  ['types', 'Activity types'],
];

const extTab = (t: { key: string; label: string }) => [t.key, t.label] as [TabKey, string];

// Extension tabs go after the core ones unless they ask to lead.
const TABS: [TabKey, string][] = [
  ...extensionAdminTabs.filter(t => t.leading).map(extTab),
  ...CORE_TABS,
  ...extensionAdminTabs.filter(t => !t.leading).map(extTab),
];

export function AdminPage() {
  const { me, isAdmin } = useAuth();
  // A non-admin's slice of Administration: only the extension tabs their
  // capabilities grant. Anyone holding neither the admin role nor such a
  // capability has no slice at all — /admin has no nav entry for them
  // (Shell.tsx) but the URL is still typeable, and every tab's API 403s, so the
  // page rendered an empty shell of em-dashes. Show the denial instead. The
  // capability is the same one the nav gates on.
  // An extension tab may carry a capability that also grants access to this
  // page. Such a principal sees ONLY the tabs they hold the capability for;
  // core tabs stay admin-only.
  const perms: Record<string, boolean> = me?.permissions || {};
  const grantedExtTabs = extensionAdminTabs.filter(t => t.cap && perms[t.cap] === true);
  const visibleTabs = isAdmin ? TABS : grantedExtTabs.map(extTab);
  const isTabKey = (t: string | null): t is TabKey => visibleTabs.some(([k]) => k === t);
  // Tab lives in the URL (like the camera Config page) so other pages can deep
  // link straight to a section — e.g. #/admin?tab=types from AI Analytics.
  const [params, setParams] = useSearchParams();
  const tab: TabKey = isTabKey(params.get('tab')) ? (params.get('tab') as TabKey) : (visibleTabs[0]?.[0] ?? '');
  const setTab = (k: TabKey) => {
    const next = new URLSearchParams(params);
    next.set('tab', k);
    setParams(next, { replace: true });
  };
  const activeExtTab = extensionAdminTabs.find(t => t.key === tab);

  if (!visibleTabs.length) {
    return (
      <div className="fade">
        <div className="card" style={{ padding: 'var(--s5)' }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>Administration is not available for your role</div>
          <div className="d-hint">
            This section needs the administrator role, or a capability granted for one of its sections.
            Ask an administrator if you need access.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="fade">
      {/* Title lives in the topbar (like Live View) — tabs lead the page. The
          primary action rides at the end of the tab row, outside the pill, and
          only on the tab where it applies. */}
      <div className="tabrow">
        <div className="tabs page-tabs">
          {visibleTabs.map(([k, label]) => (
            <div key={k} className={`tab${tab === k ? ' active' : ''}`} onClick={() => setTab(k)}>{label}</div>
          ))}
        </div>
        {activeExtTab?.action?.()}
      </div>

      {tab === 'audit' && <AuditTab />}
      {tab === 'health' && <HealthTab />}
      {tab === 'storage' && <StorageTab />}
      {tab === 'types' && <ActivityTypesTab />}
      {extensionAdminTabs.map(t => tab === t.key
        ? <div key={t.key}>{t.render()}</div>
        : null)}
    </div>
  );
}
