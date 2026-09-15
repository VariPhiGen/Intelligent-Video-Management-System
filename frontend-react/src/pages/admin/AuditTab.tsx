/**
 * AuditTab.tsx — Administration → Audit log.
 *
 * The append-only, hash-chained trail (DPDP Rule 6 / STQC Level-2). The backend
 * recomputes each row's hash on read and returns `integrity: verified|mismatch`,
 * so a row altered after the fact shows red here without any client-side crypto.
 * Filtering is server-side (`q`); "CERT-In export" downloads the filtered log as
 * CSV. Optional extensions may contribute further toolbar actions,
 * which is where the attestable CSV/XLSX/JSON copies are taken.
 */
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { apiFetch, apiDownload } from '@/lib/api';
import type { AuditEntry, AuditPage, AuditVerify } from '@/lib/types';
import { useToast } from '@/components/Toast';
import { ExtensionSlot } from '@/extensions';

type SearchSide = { indexing?: boolean; domains?: string[] };

/** " · search: person, vehicles → face, person, vehicles". Switching `face` on
 *  starts collecting biometric data, so the row names the domains rather than
 *  saying only that search settings moved. Entries written before 2026-09-15
 *  carry no `search` detail and read exactly as they did. */
function searchChange(s?: { before?: SearchSide; after?: SearchSide }): string {
  if (!s?.before || !s?.after) return '';
  const side = (x: SearchSide) =>
    !x.indexing ? 'off' : x.domains?.length ? x.domains.join(', ') : 'none';
  return ` · search: ${side(s.before)} → ${side(s.after)}`;
}

// Machine action code → human sentence, mirroring the prototype's phrasing.
// The target and structured detail become the "· …" suffix.
export function formatAction(e: AuditEntry): string {
  const t = e.target;
  const d = e.detail || {};
  const suffix = t ? ` · ${t}` : '';
  switch (e.action) {
    case 'evidence.export_requested': return `Evidence export requested${suffix}`;
    case 'report.generated': return 'Report generated';
    case 'alarm.acknowledged': return `Alarm acknowledged${suffix}`;
    case 'camera.config_changed':
      return `Camera config changed${d.section ? ` · ${d.section}` : ''}${searchChange(d.search)}${suffix}`;
    case 'camera.masks_changed': return `Privacy masks changed${suffix}`;
    case 'camera.created': return `Camera added${suffix}`;
    case 'camera.deleted': return `Camera deleted${suffix}`;
    case 'retention.changed':
      return `Retention changed${d.retention_days != null ? ` · ${d.retention_days} days` : ' · reset to default'}`;
    case 'user.created': return `User invited${suffix}`;
    case 'user.updated': return `User updated${suffix}`;
    case 'user.deleted': return `User deleted${suffix}`;
    case 'user.password_reset': return `Password reset${suffix}`;
    case 'auth.login': return 'Signed in';
    case 'auth.logout': return 'Signed out';
    case 'auth.password_changed': return `Password changed${suffix}`;
    case 'role.updated': return `Role updated${suffix}`;
    case 'role.policy_changed': return `Permissions changed${suffix}`;
    case 'face.search': return `Face search${d.basis ? ` · basis: ${d.basis}` : ''}`;
    case 'auth.login_failed':
      return `Failed login${d.error ? ` · ${d.error}` : ''}${d.locked ? ' · account locked' : ''}`;
    default: return `${e.action}${suffix}`;
  }
}

function Actor({ e }: { e: AuditEntry }) {
  if (e.actor_type === 'user' && e.actor) return <span>{e.actor}</span>;
  // system / unknown / service — a non-human actor, shown muted.
  return <span style={{ color: 'var(--dim)', fontStyle: 'italic' }}>{e.actor || e.actor_type}</span>;
}

const PAGE_SIZE = 50;

export function AuditTab() {
  const toast = useToast();
  // `?q=` seeds the filter so other pages can hand off a scoped view — the
  // per-camera Audit tab in camera config deep links here with the slug.
  // Consumed once and stripped from the URL below, so switching tabs and
  // coming back doesn't silently re-apply someone else's filter.
  const [sp, setSp] = useSearchParams();
  const [raw, setRaw] = useState(() => sp.get('q') || '');   // filter box value
  const [q, setQ] = useState(() => sp.get('q') || '');       // debounced, sent to the server
  const [pageIdx, setPageIdx] = useState(0);   // zero-based page number
  const [page, setPage] = useState<AuditPage | null>(null);
  const [chain, setChain] = useState<AuditVerify | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    if (!sp.get('q')) return;
    const next = new URLSearchParams(sp);
    next.delete('q');
    setSp(next, { replace: true });
  }, [sp, setSp]);

  // Debounce the filter so we don't refetch on every keystroke; a new filter
  // also jumps back to the first page (set together so it's one refetch).
  useEffect(() => {
    const h = setTimeout(() => { setQ(raw.trim()); setPageIdx(0); }, 300);
    return () => clearTimeout(h);
  }, [raw]);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const params = new URLSearchParams({
          limit: String(PAGE_SIZE),
          offset: String(pageIdx * PAGE_SIZE),
        });
        if (q) params.set('q', q);
        const [p, v] = await Promise.all([
          apiFetch<AuditPage>(`/audit?${params}`),
          apiFetch<AuditVerify>('/audit/verify'),
        ]);
        if (!live) return;
        setPage(p);
        setChain(v);
        setErr('');
      } catch (e: any) {
        if (live) setErr(e.message);
      }
    })();
    return () => { live = false; };
  }, [q, pageIdx]);

  async function exportCsv() {
    try {
      const qs = q ? `?format=csv&q=${encodeURIComponent(q)}` : '?format=csv';
      await apiDownload(`/audit/export${qs}`, 'audit-log.csv');
    } catch (e: any) { toast(e.message, 'err'); }
  }

  const entries = page?.entries ?? [];
  const total = page?.total ?? 0;
  const from = total === 0 ? 0 : pageIdx * PAGE_SIZE + 1;
  const to = Math.min((pageIdx + 1) * PAGE_SIZE, total);
  const hasNext = (pageIdx + 1) * PAGE_SIZE < total;
  const chainNote = useMemo(() => {
    if (!chain) return '';
    return chain.ok
      ? 'chain verified'
      : `⚠ integrity broken at entry #${chain.first_broken_id}`;
  }, [chain]);

  return (
    <div>
      {/* Toolbar: filter + exports (mirrors the prototype header row). */}
      <div style={{ display: 'flex', gap: 10, alignItems: 'center', marginBottom: 14 }}>
        <input
          value={raw}
          onChange={e => setRaw(e.target.value)}
          placeholder="Filter by user, action, target…"
          style={{ flex: 1, maxWidth: 480 }}
        />
        <div style={{ flex: 1 }} />
        <button className="btn-subtle btn-sm" onClick={exportCsv}>↓ CERT-In export</button>
        {/* Optional extensions may add actions here; none are part of the open
            core. Renders nothing if none are present. */}
        <ExtensionSlot name="audit.toolbar" />
      </div>

      <div className="panel">
        <div className="panel-body flush">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 150 }}>Time</th>
                  <th style={{ width: 160 }}>User</th>
                  <th>Action</th>
                  <th style={{ width: 130 }}>Source IP</th>
                  <th style={{ width: 120 }}>Integrity</th>
                </tr>
              </thead>
              <tbody>
                {err && <tr><td colSpan={5}><div className="empty">{err}</div></td></tr>}
                {!err && page === null && (
                  <tr><td colSpan={5}><div className="empty"><span className="spinner" /> <span style={{ marginLeft: 8 }}>Loading audit trail…</span></div></td></tr>
                )}
                {!err && page !== null && !entries.length && (
                  <tr><td colSpan={5} style={{ padding: 0 }}>
                    <div className="emptystate" style={{ border: 'none', borderRadius: 0 }}>
                      <div className="glyph">🛡</div>
                      <h4>{q ? 'No matching entries' : 'No audit entries yet'}</h4>
                      <p>{q ? 'Try a different filter.' : 'Actions like exports, config changes and role grants will appear here.'}</p>
                    </div>
                  </td></tr>
                )}
                {!err && entries.map(e => {
                  const dt = new Date(e.ts);
                  const time = dt.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
                  const date = dt.toLocaleDateString([], { month: 'short', day: 'numeric' });
                  const bad = e.integrity === 'mismatch';
                  return (
                    <tr key={e.id} style={bad ? { background: 'var(--redSoft)' } : undefined}>
                      <td className="mono" style={{ fontSize: 12.5 }} title={dt.toLocaleString()}>
                        <span style={{ fontWeight: 600 }}>{time}</span>
                        <span style={{ color: 'var(--dim)', marginLeft: 6 }}>{date}</span>
                      </td>
                      <td style={{ fontSize: 12.5 }}><Actor e={e} /></td>
                      <td style={{ fontSize: 12.5 }}>{formatAction(e)}</td>
                      <td className="mono" style={{ fontSize: 12, color: e.source_ip ? 'var(--text2)' : 'var(--dim)' }}>
                        {e.source_ip || '—'}
                      </td>
                      <td>
                        {bad
                          ? <span className="badge badge-red"><span className="badge-dot" />Mismatch</span>
                          : <span className="badge badge-green"><span className="badge-dot" />Verified</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Pagination */}
      {page && total > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, marginTop: 12 }}>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            Showing <b>{from}</b>–<b>{to}</b> of <b>{total}</b> entr{total === 1 ? 'y' : 'ies'}
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="btn-subtle btn-sm" disabled={pageIdx === 0}
              onClick={() => setPageIdx(p => Math.max(0, p - 1))}>← Prev</button>
            <button className="btn-subtle btn-sm" disabled={!hasNext}
              onClick={() => setPageIdx(p => p + 1)}>Next →</button>
          </div>
        </div>
      )}

      <div className="d-hint" style={{ marginTop: 12 }}>
        Append-only, hash-chained · integrity re-verified on load
        {chainNote && <> · {chainNote}</>}
      </div>
    </div>
  );
}
