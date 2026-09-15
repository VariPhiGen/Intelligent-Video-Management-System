/**
 * WallView.tsx — NOC video wall (legacy liveRenderWall): 3×3 monitor array
 * with header controls (online blip, N/9 counter, push/clear/fullscreen).
 * Cell contents are resolved camera objects; clicks are handled by the parent
 * (filled → clear, empty → hint toast).
 */
import { useRef } from 'react';
import type { Camera } from '@/lib/types';
import { Clock, LiveVideo, LvBadges } from './LiveVideo';

export function WallView({ cells, onCellClick, onClear, onPushFromLive }: {
  cells: (Camera | null)[];
  onCellClick: (i: number) => void;
  onClear: () => void;
  onPushFromLive: () => void;
}) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const used = cells.filter(Boolean).length;
  let liveIdx = 0; // stagger index among filled monitors only

  return (
    /* id reuses the #live-grid:fullscreen styling from global.css */
    <div id="live-grid" ref={rootRef}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600 }}>NOC Video Wall</div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 2 }}>Control room · 3×3 monitor array</div>
        </div>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--green)' }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--green)', animation: 'blip 1.6s infinite' }} />
          Wall online
        </span>
        <span style={{ fontSize: 11, color: 'var(--muted)', background: 'var(--input)', border: '1px solid var(--border2)',
                       borderRadius: 20, padding: '4px 11px' }}>
          {used} / 9 monitors active
        </span>
        <div style={{ flex: 1 }} />
        <button className="btn-ghost btn-sm" onClick={onPushFromLive}>＋ Push from Live</button>
        <button className="btn-ghost btn-sm" onClick={onClear}>Clear wall</button>
        <button className="btn-primary btn-sm"
                onClick={() => { const el = rootRef.current; if (el?.requestFullscreen) el.requestFullscreen(); }}>
          Fullscreen
        </button>
      </div>
      <div className="wall-stage">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6 }}>
          {cells.map((c, i) => {
            if (!c) {
              return (
                <div key={`m-${i}`} className="wall-cell wall-empty" onClick={() => onCellClick(i)}>
                  <div style={{ textAlign: 'center' }}>
                    <div style={{ fontFamily: 'var(--mono)', fontSize: 10, color: '#3d4c5c' }}>MONITOR {i + 1}</div>
                    <div style={{ fontSize: 10, color: '#31404f', marginTop: 3 }}>free</div>
                  </div>
                </div>
              );
            }
            return (
              <div key={c.id} className="wall-cell" onClick={() => onCellClick(i)} title="Click to clear this monitor">
                <LiveVideo slug={c.slug} variant="wall" staggerMs={liveIdx++ * 250}
                            hasSub={!!c.sub_track?.url_raw} />
                <div style={{ position: 'absolute', left: 7, top: 6, fontFamily: 'var(--mono)', fontSize: 9,
                              color: 'rgba(220,227,236,.75)' }}>
                  M{i + 1}
                </div>
                <LvBadges c={c} />
                <div style={{ position: 'absolute', left: 8, bottom: 7, pointerEvents: 'none' }}>
                  <div style={{ fontSize: 10.5, fontWeight: 500, color: '#fff', textShadow: '0 1px 3px rgba(0,0,0,.7)' }}>{c.name}</div>
                  <div style={{ fontFamily: 'var(--mono)', fontSize: 8.5, color: '#DCE3EC' }}>
                    {c.slug} · <Clock />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
