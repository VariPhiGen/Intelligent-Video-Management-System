/**
 * LiveVideo.tsx — shared building blocks for the Live page views: the HLS
 * <video> with its "Signal lost" overlay (legacy lv-/lvoff- pair), the
 * LIVE/REC/OFFLINE badge cluster (_lvBadges) and the ticking clock (.lv-time).
 */
import { useEffect, useState, type CSSProperties } from 'react';
import type { Camera } from '@/lib/types';
import { useHls } from './useHls';

type Variant = 'tile' | 'focus' | 'wall';

// `objectFit: 'contain'` EVERYWHERE, including the wall. The cell is a fixed
// 16/9 box (.wall-cell in global.css), so `cover` cropped any camera whose
// picture is not 16:9 — a 4:3 or corridor-mode portrait stream lost its edges
// silently, with nothing on screen to say part of the scene was missing. On a
// surveillance wall that is the one failure the operator cannot detect by
// looking: the picture is sharp, correctly framed, and incomplete.
//
// `contain` letterboxes instead. For a 16:9 camera the two are identical, which
// is why this shipped — every camera on the appliance it was written against
// was 16:9. The other two variants were already `contain`; the wall was the
// outlier, not the rule.
const VIDEO_STYLE: Record<Variant, CSSProperties> = {
  tile: { width: '100%', height: '100%', objectFit: 'contain', background: '#0a0e13' },
  focus: { position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'contain', background: '#0a0e13' },
  wall: { width: '100%', height: '100%', objectFit: 'contain', background: '#0a0e13' },
};

const OFFLINE_META: Record<Variant, { icon: number; text: number; label: string }> = {
  tile: { icon: 22, text: 10.5, label: 'Signal lost' },
  focus: { icon: 26, text: 11, label: 'Signal lost — retrying' },
  wall: { icon: 18, text: 9.5, label: 'Signal lost' },
};

export function LiveVideo({ slug, variant, staggerMs = 0, hasSub = false }: {
  slug: string;
  variant: Variant;
  staggerMs?: number;
  /** Camera has a recording sub track, so the relay publishes `<slug>_sub`. */
  hasSub?: boolean;
}) {
  const { videoRef, offline, variant: stream } = useHls(slug, staggerMs, hasSub);
  const off = OFFLINE_META[variant];
  return (
    <>
      <video ref={videoRef} muted playsInline autoPlay style={VIDEO_STYLE[variant]} />
      {/* Say so when the picture is the low-resolution stream. An operator
          judging detail — a face, a plate — needs to know they are not looking
          at the camera's full quality, and the fallback is silent otherwise. */}
      {stream === 'sub' && !offline && (
        <span className="lv-badge" style={{ position: 'absolute', left: 8, top: 8,
                                            fontSize: variant === 'wall' ? 9 : 10 }}>
          LOW-RES
        </span>
      )}
      <div style={{ position: 'absolute', inset: 0, display: offline ? 'flex' : 'none',
                    alignItems: 'center', justifyContent: 'center', background: '#0a0e13' }}>
        <div style={{ textAlign: 'center', color: '#5b7186' }}>
          <div style={{ fontSize: off.icon }}>⚠</div>
          <div style={{ fontSize: off.text, marginTop: variant === 'wall' ? 0 : 4 }}>{off.label}</div>
        </div>
      </div>
    </>
  );
}

/** LIVE / REC / OFFLINE badge cluster, top-right of a tile (legacy _lvBadges). */
export function LvBadges({ c }: { c: Camera }) {
  const online = c.health_status === 'connected';
  return (
    <div style={{ position: 'absolute', right: 8, top: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
      {online && (
        <span className="lv-badge">
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#22C55E' }} />LIVE
        </span>
      )}
      {online && c.recording !== false && (
        <span className="lv-badge">
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#EF4444', animation: 'blip 1.4s infinite' }} />REC
        </span>
      )}
      {!online && <span className="lv-badge" style={{ color: '#5b7186' }}>OFFLINE</span>}
    </div>
  );
}

const clockNow = () => new Date().toLocaleTimeString([], { hour12: false });

/** Per-tile wall-clock, ticking every second (legacy .lv-time updater). */
export function Clock() {
  const [now, setNow] = useState(clockNow);
  useEffect(() => {
    const t = setInterval(() => setNow(clockNow()), 1000);
    return () => clearInterval(t);
  }, []);
  return <span className="lv-time">{now}</span>;
}
