/**
 * WallPicker.tsx — choose exactly which cameras the video wall shows: search, cameras grouped by
 * zone with checkboxes, Select all / Deselect all / per-zone toggles, and a snapshot thumbnail per
 * camera so operators pick by picture, not by name.
 *
 * Thumbnails are LAZY and THROTTLED: each one is an on-demand ffmpeg decode on the appliance
 * (/api/cameras/{id}/snapshot), so they load only when scrolled into view, at most 3 at a time —
 * opening the picker on a 60-camera site must not fire 60 decodes at once.
 *
 * "All cameras" is a MODE, not a one-off Select-all: the wall becomes "every enabled camera", so a
 * camera added next week shows up without anyone re-opening this picker (with offline ones hidden,
 * that's "every live camera"). Ticking it greys out the per-camera list.
 *
 * Admins also get "Save as the default for everyone" (on by default — an admin choosing the wall's
 * cameras is almost always setting up THE wall); unticked, it changes only this browser, like it
 * does for operators.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Modal } from '@/components/Modal';
import { apiBlob } from '@/lib/api';
import { zoneOf } from '@/lib/cameras';
import type { Camera } from '@/lib/types';

const MAX_PARALLEL = 3;
let active = 0;
const queue: (() => void)[] = [];
function schedule(job: () => Promise<void>) {
  const run = () => { active++; job().finally(() => { active--; queue.shift()?.(); }); };
  if (active < MAX_PARALLEL) run(); else queue.push(run);
}

function Thumb({ cam }: { cam: Camera }) {
  const ref = useRef<HTMLDivElement>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let cancelled = false;
    let made: string | null = null;
    const io = new IntersectionObserver(entries => {
      if (!entries.some(e => e.isIntersecting)) return;
      io.disconnect();
      schedule(async () => {
        if (cancelled) return;
        try {
          const blob = await apiBlob(`/cameras/${cam.id}/snapshot`);
          if (cancelled) return;
          made = URL.createObjectURL(blob);
          setUrl(made);
        } catch { if (!cancelled) setFailed(true); }
      });
    }, { rootMargin: '80px' });
    io.observe(el);
    return () => { cancelled = true; io.disconnect(); if (made) URL.revokeObjectURL(made); };
  }, [cam.id]);
  return (
    <div ref={ref} className="wp-thumb">
      {url ? <img src={url} alt="" />
        : <span>{failed ? 'no preview' : cam.health_status === 'connected' ? 'loading…' : 'offline'}</span>}
    </div>
  );
}

export function WallPicker({ open, cameras, selected, followAll, isAdmin, onApply, onClose }: {
  open: boolean;
  cameras: Camera[];
  selected: string[];
  followAll: boolean;
  isAdmin: boolean;
  onApply: (ids: string[], followAll: boolean, forEveryone: boolean) => void;
  onClose: () => void;
}) {
  const [picked, setPicked] = useState<Set<string>>(new Set(selected));
  const [all, setAll] = useState(followAll);
  const [forEveryone, setForEveryone] = useState(true);
  const [q, setQ] = useState('');
  useEffect(() => {
    if (open) {
      setPicked(new Set(followAll ? cameras.map(c => c.id) : selected)); setAll(followAll); setQ(''); setForEveryone(true);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const shown = useMemo(() => {
    const s = q.trim().toLowerCase();
    return cameras.filter(c => !s || c.name.toLowerCase().includes(s) || c.slug.toLowerCase().includes(s)
      || zoneOf(c).toLowerCase().includes(s));
  }, [cameras, q]);
  const zones = useMemo(() => {
    const g: Record<string, Camera[]> = {};
    shown.forEach(c => { (g[zoneOf(c)] = g[zoneOf(c)] || []).push(c); });
    return g;
  }, [shown]);

  const toggle = (id: string) => setPicked(p => { const n = new Set(p); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const setMany = (ids: string[], on: boolean) => setPicked(p => {
    const n = new Set(p); ids.forEach(id => (on ? n.add(id) : n.delete(id))); return n;
  });
  const apply = () => {
    // Keep the current wall order for cameras that stay; append newly picked ones in list order.
    const keep = selected.filter(id => picked.has(id));
    const added = cameras.map(c => c.id).filter(id => picked.has(id) && !keep.includes(id));
    onApply(all ? [] : [...keep, ...added], all, isAdmin && forEveryone);
  };

  return (
    <Modal open={open} title="Cameras on the wall" onClose={onClose} width={860}
           footer={(
             <div style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%' }}>
               <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                 {all ? `All ${cameras.length} cameras, and any added later` : `${picked.size} of ${cameras.length} selected`}
               </span>
               <div style={{ flex: 1 }} />
               {isAdmin && (
                 <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12 }}
                        title="Unticked: only this browser changes">
                   <input type="checkbox" style={{ width: 'auto' }} checked={forEveryone}
                          onChange={e => setForEveryone(e.target.checked)} />
                   Save as the default for everyone
                 </label>
               )}
               <button className="btn-ghost btn-sm" onClick={onClose}>Cancel</button>
               <button className="btn-primary btn-sm" onClick={apply} disabled={!all && !picked.size}>
                 {all ? 'Show all cameras' : `Show ${picked.size || ''} on wall`}
               </button>
             </div>
           )}>
      <label className="wp-zone" style={{ marginBottom: 12, padding: '8px 10px', borderRadius: 'var(--r-md)',
                                          border: '1px solid var(--border2)', alignItems: 'flex-start' }}>
        <input type="checkbox" checked={all} style={{ marginTop: 2 }} onChange={e => setAll(e.target.checked)} />
        <span>
          All cameras — keep the wall in sync automatically
          <span style={{ display: 'block', fontWeight: 400, fontSize: 11.5, color: 'var(--muted)' }}>
            New cameras join on their own; with “Hide offline cameras” on (⚙), the wall shows every camera that is live.
          </span>
        </span>
      </label>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12,
                    ...(all ? { opacity: .45, pointerEvents: 'none' } : {}) }}>
        <div className="inv-search" style={{ flex: 1, minWidth: 200, maxWidth: 'none' }}>
          <span className="ico">⌕</span>
          <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search cameras or zones…" />
        </div>
        <button className="btn-ghost btn-sm" onClick={() => setMany(shown.map(c => c.id), true)}>Select all{q ? ' shown' : ''}</button>
        <button className="btn-ghost btn-sm" onClick={() => (q ? setMany(shown.map(c => c.id), false) : setPicked(new Set()))}>
          Deselect all{q ? ' shown' : ''}
        </button>
      </div>
      <div style={all ? { opacity: .45, pointerEvents: 'none' } : undefined} aria-disabled={all}>
      {Object.keys(zones).sort().map(z => {
        const ids = zones[z].map(c => c.id);
        const allOn = ids.every(id => picked.has(id));
        return (
          <div key={z} style={{ marginBottom: 14 }}>
            <label className="wp-zone">
              <input type="checkbox" checked={allOn}
                     ref={el => { if (el) el.indeterminate = !allOn && ids.some(id => picked.has(id)); }}
                     onChange={() => setMany(ids, !allOn)} />
              <span>{z}</span><span className="cfg-count">{ids.length}</span>
            </label>
            <div className="wp-grid">
              {zones[z].map(c => (
                <label key={c.id} className={`wp-cam${picked.has(c.id) ? ' on' : ''}`}>
                  <Thumb cam={c} />
                  <div className="wp-meta">
                    <input type="checkbox" checked={picked.has(c.id)} onChange={() => toggle(c.id)} />
                    <span className="wp-name" title={c.name}>{c.name}</span>
                    <span style={{ width: 7, height: 7, borderRadius: '50%', flexShrink: 0,
                                   background: c.health_status === 'connected' ? 'var(--green)' : 'var(--red)' }} />
                  </div>
                </label>
              ))}
            </div>
          </div>
        );
      })}
      </div>
      {!shown.length && <div style={{ color: 'var(--muted)', fontSize: 13 }}>No cameras match “{q}”.</div>}
    </Modal>
  );
}
