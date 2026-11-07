/**
 * CameraPicker.tsx — choose a camera for one wall monitor.
 *
 * WHY IT EXISTS. Putting a camera on the wall used to mean: leave the wall, go
 * to Live grid, find the camera among all of them, press "Push to wall", and
 * accept whichever monitor the wall happened to fill first. That is a workable
 * flow for nine cameras and an unusable one for a hundred, and it never let an
 * operator say WHICH monitor. Here the monitor is already chosen — the operator
 * clicked it — so this answers one question and closes.
 *
 * Search matches name, slug and zone, because an operator hunting a camera at a
 * hundred-camera site knows one of those three and not reliably which.
 *
 * Cameras already on the wall stay listed rather than being hidden: "where is
 * that camera" is exactly the question being asked, and an answer of "not in
 * this list" is worse than one that says "on Monitor 4". Picking one moves it.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Modal } from '@/components/Modal';
import { zoneOf } from '@/lib/cameras';
import type { Camera } from '@/lib/types';

export function CameraPicker({ open, monitor, cameras, placed, current, onPick, onRemove, onClose }: {
  open: boolean;
  /** Zero-based cell index; displayed one-based, like every other monitor label. */
  monitor: number;
  cameras: Camera[];
  /** camera id → monitor index it currently occupies. */
  placed: Map<string, number>;
  /** What is on this monitor now, if anything. */
  current: Camera | null;
  onPick: (cameraId: string) => void;
  onRemove: () => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const searchRef = useRef<HTMLInputElement>(null);

  // Fresh query each time, and the caret already in the box: this opens from a
  // click on a monitor, and the next thing the operator does is type a name.
  useEffect(() => {
    if (!open) return;
    setQ('');
    const t = window.setTimeout(() => searchRef.current?.focus(), 30);
    return () => window.clearTimeout(t);
  }, [open, monitor]);

  const matches = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const pool = cameras.filter(c => c.enabled);
    if (!needle) return pool;
    return pool.filter(c =>
      c.name.toLowerCase().includes(needle)
      || c.slug.toLowerCase().includes(needle)
      || zoneOf(c).toLowerCase().includes(needle));
  }, [cameras, q]);

  return (
    <Modal open={open} onClose={onClose} width={520}
           title={`Monitor ${monitor + 1}`}>
      {/* REMOVAL LIVES HERE, not on the wall. Clicking a live monitor opens
          this, so add, swap and take-off-air are one affordance with one thing
          to learn — and none of them can happen by brushing a feed. */}
      {current && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10,
                      padding: '9px 10px', marginBottom: 'var(--s3)',
                      background: 'var(--input)', border: '1px solid var(--border2)',
                      borderRadius: 'var(--r-sm)' }}>
          <span style={{ minWidth: 0, flex: 1 }}>
            <span style={{ display: 'block', fontSize: 12.5, color: 'var(--text)',
                           overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {current.name}
            </span>
            <span style={{ display: 'block', fontSize: 10.5, color: 'var(--dim)' }}>
              on air now
            </span>
          </span>
          <button className="btn-ghost btn-sm" onClick={onRemove}>Remove camera</button>
        </div>
      )}
      <div className="inv-search" style={{ maxWidth: 'none', marginBottom: 'var(--s3)' }}>
        <span className="ico">⌕</span>
        <input ref={searchRef} value={q} onChange={e => setQ(e.target.value)}
               placeholder="Search by name, zone or slug…"
               onKeyDown={e => {
                 // Enter takes the only match. With one camera left on screen,
                 // reaching for the mouse to click the single row is friction
                 // with nothing to decide.
                 if (e.key === 'Enter' && matches.length === 1) onPick(matches[0].id);
               }} />
      </div>

      {!matches.length ? (
        <div className="emptystate" style={{ padding: 'var(--s6) var(--s4)' }}>
          <div className="glyph">▦</div>
          <h4>{q ? 'No camera matches that' : 'No cameras are enabled'}</h4>
          <p>{q
            ? 'Search by camera name, zone or slug.'
            : 'Enable a camera on the Cameras page and it will appear here.'}</p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 1, maxHeight: 420 }}>
          {matches.map(c => {
            const on = placed.get(c.id);
            const here = on === monitor;
            return (
              <button key={c.id} className="lv-node" onClick={() => onPick(c.id)}
                      disabled={here}
                      style={{ display: 'flex', alignItems: 'center', gap: 9, width: '100%',
                               textAlign: 'left', background: 'transparent', border: 0,
                               padding: '8px 10px', borderRadius: 'var(--r-sm)',
                               cursor: here ? 'default' : 'pointer', opacity: here ? 0.5 : 1 }}>
                <span style={{ width: 6, height: 6, borderRadius: '50%', flex: '0 0 auto',
                               background: c.health_status === 'connected' ? 'var(--green)' : 'var(--red)' }} />
                <span style={{ minWidth: 0, flex: 1 }}>
                  <span style={{ display: 'block', fontSize: 13, color: 'var(--text)',
                                 overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {c.name}
                  </span>
                  <span style={{ display: 'block', fontFamily: 'var(--mono)', fontSize: 10,
                                 color: 'var(--dim)', overflow: 'hidden',
                                 textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {zoneOf(c)} · {c.slug}
                  </span>
                </span>
                {/* Where it already is, so "move it here" is a decision the
                    operator makes knowingly rather than discovers afterwards. */}
                {on !== undefined && (
                  <span className="cfg-count" style={{ flex: '0 0 auto' }}>
                    {here ? 'here' : `on M${on + 1}`}
                  </span>
                )}
                {!c.sub_track?.url_raw && (
                  <span className="cfg-count" style={{ flex: '0 0 auto' }}>full-res only</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </Modal>
  );
}
