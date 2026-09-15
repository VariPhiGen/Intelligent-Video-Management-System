/**
 * CameraRail.tsx — multi-camera playback's right rail: which cameras are in
 * view, the searchable picker that adds one, and the case actions.
 *
 * Extracted from MultiPlayer because it is the only part of that screen that
 * does not touch the shared clock — it edits a list of four slugs. The sync
 * lock indicator is static on purpose: every tile is always on one clock, so
 * there is nothing to toggle (see the disabled case buttons below it, which are
 * the Incident Cases surface that does not exist yet).
 */
import { useMemo } from 'react';

import type { NvrCamera } from './coverage';

export function CameraRail({
  sel, nvrCams, regName, toggleCam, camFilter, setCamFilter, addOpen, setAddOpen,
}: {
  sel: string[];
  nvrCams: NvrCamera[];
  regName: (slug: string) => string;
  toggleCam: (slug: string) => void;
  camFilter: string;
  setCamFilter: (v: string) => void;
  addOpen: boolean;
  setAddOpen: (v: boolean) => void;
}) {
  // Cameras that can still be added: recorded, not already in view, and
  // matching whatever has been typed.
  const addCandidates = useMemo(() => {
    const q = camFilter.trim().toLowerCase();
    return nvrCams.filter(c =>
      !sel.includes(c.name) &&
      (!q || c.name.toLowerCase().includes(q) || regName(c.name).toLowerCase().includes(q)));
  }, [nvrCams, sel, camFilter, regName]);

  return (
    <div className="card" style={{ padding: 14 }}>
      <div style={{ fontSize: 10.5, fontWeight: 600, letterSpacing: '.12em', textTransform: 'uppercase',
                    color: 'var(--dim)', marginBottom: 10 }}>
        Cameras in view <span style={{ color: 'var(--dim)', fontWeight: 500 }}>({sel.length}/4)</span>
      </div>
      {/* Only the selected cameras show as chips — each removable with ✕. */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 12 }}>
        {!sel.length && (
          <span style={{ color: 'var(--dim)', fontSize: 12 }}>
            {nvrCams.length ? 'No cameras selected — add one below.' : 'No recorded cameras yet.'}
          </span>
        )}
        {sel.map(name => (
          <button key={name} className="chip active" title="Remove from view"
                  style={{ justifyContent: 'space-between', width: '100%', flexShrink: 0 }}
                  onClick={() => toggleCam(name)}>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {regName(name)}
            </span>
            <span style={{ color: 'var(--dim)', fontSize: 14, lineHeight: 1 }}>✕</span>
          </button>
        ))}
      </div>

      {/* Searchable add-camera picker — hidden once 4 are selected. */}
      {sel.length >= 4 ? (
        <div className="d-hint" style={{ margin: '0 0 14px' }}>
          Maximum 4 cameras — remove one to add another.
        </div>
      ) : nvrCams.length > 0 && (
        <div style={{ position: 'relative', marginBottom: 14 }}>
          <input
            value={camFilter}
            onChange={e => { setCamFilter(e.target.value); setAddOpen(true); }}
            onFocus={() => setAddOpen(true)}
            onBlur={() => setTimeout(() => setAddOpen(false), 120)}
            onKeyDown={e => { if (e.key === 'Escape') { setAddOpen(false); (e.target as HTMLInputElement).blur(); } }}
            placeholder="＋ Add camera…"
            style={{ width: '100%', fontSize: 12 }}
          />
          {addOpen && (
            <div style={{
              position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 20, marginTop: 4,
              background: 'var(--surface)', border: '1px solid var(--border2)', borderRadius: 8,
              boxShadow: '0 8px 24px rgba(0,0,0,.35)', maxHeight: 260, overflowY: 'auto', padding: 4,
            }}>
              {!addCandidates.length && (
                <div style={{ color: 'var(--dim)', fontSize: 12, padding: '6px 8px' }}>
                  {camFilter.trim() ? `No cameras match “${camFilter}”.` : 'All cameras are already in view.'}
                </div>
              )}
              {addCandidates.map(c => (
                <button key={c.name}
                        // mousedown fires before the input's blur, so the add
                        // registers without the dropdown closing out from under it.
                        onMouseDown={e => { e.preventDefault(); toggleCam(c.name); setCamFilter(''); }}
                        onMouseEnter={e => { e.currentTarget.style.background = 'var(--accentSoft)'; }}
                        onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; }}
                        style={{
                          display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%',
                          padding: '7px 9px', borderRadius: 6, border: 'none', background: 'transparent',
                          cursor: 'pointer', fontSize: 12.5, textAlign: 'left', color: 'var(--text)',
                        }}>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {regName(c.name)}
                  </span>
                  <span style={{ color: 'var(--dim)', fontSize: 13, flexShrink: 0, marginLeft: 8 }}>＋</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '4px 0 14px', fontSize: 12,
                    color: 'var(--green)' }}>
        <span style={{ width: 30, height: 16, borderRadius: 8, background: 'var(--green)',
                       position: 'relative', flexShrink: 0 }}>
          <span style={{ position: 'absolute', top: 2, right: 2, width: 12, height: 12,
                         borderRadius: '50%', background: '#fff' }} />
        </span>
        Sync lock · all at same time
      </div>
      <button className="btn-ghost btn-sm" disabled style={{ width: '100%', marginBottom: 8 }}>
        ＋ Start incident case
      </button>
      <button className="btn-ghost btn-sm" disabled style={{ width: '100%' }}>
        🔖 Bookmark this moment
      </button>
    </div>
  );
}
