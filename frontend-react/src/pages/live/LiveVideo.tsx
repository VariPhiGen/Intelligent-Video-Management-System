/**
 * LiveVideo.tsx — shared building blocks for the Live page views: the HLS
 * <video> with its "Signal lost" overlay (legacy lv-/lvoff- pair), the
 * LIVE/REC/OFFLINE badge cluster (_lvBadges) and the ticking clock (.lv-time).
 */
import { useEffect, useState, type CSSProperties } from 'react';
import type { Camera } from '@/lib/types';
import { useHls } from './useHls';
import { causeText, positionLabel, type PtzPosition } from '@/lib/ptz';

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

const OFFLINE_META: Record<Variant, { icon: number; text: number; label: string; codec: string }> = {
  tile: { icon: 22, text: 10.5, label: 'Signal lost', codec: 'H.265 — not playable in this browser' },
  focus: { icon: 26, text: 11, label: 'Signal lost — retrying', codec: 'H.265 — not playable in this browser' },
  wall: { icon: 18, text: 9.5, label: 'Signal lost', codec: 'H.265 not supported' },
};

// What to do about it. Not shown on the wall — a cell is too small to read it,
// and the wall is watched, not configured.
const CODEC_HINT = "Set the camera's sub stream to H.264, or use a browser with H.265 support";

export function LiveVideo({ slug, variant, staggerMs = 0, hasSub = false,
                           preferSub = false }: {
  slug: string;
  variant: Variant;
  staggerMs?: number;
  /** Camera has a recording sub track, so the relay publishes `<slug>_sub`. */
  hasSub?: boolean;
  /** Ask for the low-res track up front — a tile too small to show the main
   *  stream's detail should not pay to decode it. Falls up to the main when
   *  the camera has no sub track. */
  preferSub?: boolean;
}) {
  // Focus = the expanded / PTZ view: the operator steers by the picture, so it
  // runs the low-latency HLS profile AND attempts WebRTC first (sub-second when
  // the browser and LAN allow it, HLS fallback otherwise). Tiles and the wall
  // stay buffered — on a monitoring wall a frozen tile costs more than latency.
  const isFocus = variant === 'focus';
  const { videoRef, offline, variant: stream, unsupportedCodec } =
    useHls(slug, staggerMs, hasSub, preferSub, isFocus, isFocus);
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
          <div style={{ fontSize: off.text, marginTop: variant === 'wall' ? 0 : 4 }}>
            {unsupportedCodec ? off.codec : off.label}
          </div>
          {unsupportedCodec && variant !== 'wall' && (
            <div style={{ fontSize: off.text - 1, marginTop: 4, padding: '0 12px', opacity: 0.8 }}>
              {CODEC_HINT}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/** LIVE / REC / OFFLINE badge cluster, top-right of a tile (legacy _lvBadges). `pos` = where a PTZ camera is
 *  pointing ("📍 Gate" / "Moving…" / "Off preset" — services/ptz_track), shown first. */
export function LvBadges({ c, pos }: { c: Camera; pos?: PtzPosition | null }) {
  const online = c.health_status === 'connected';
  // 'unknown' is "not probed yet" — the state every camera is in for the
  // first ~40 s after the api restarts. Drawn as OFFLINE, an upgrade turned
  // the whole wall red for that long with every camera actually streaming.
  if (c.health_status === 'unknown') {
    return (
      <div style={{ position: 'absolute', right: 8, top: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
        <span className="lv-badge" style={{ color: '#8aa0b4' }} title="Status not checked yet">CHECKING</span>
      </div>
    );
  }
  return (
    <div style={{ position: 'absolute', right: 8, top: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
      {pos && <PtzPosBadge pos={pos} />}
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

/** Where a PTZ camera points, with who/what put it there in the tooltip. */
export function PtzPosBadge({ pos }: { pos: PtzPosition }) {
  const who = causeText(pos.cause, pos.actor);
  const since = pos.since ? new Date(pos.since).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
  return (
    <span className={`ptz-pos-badge${pos.state === 'off_preset' ? ' off' : ''}`}
          title={`${positionLabel(pos)}${since ? ` since ${since}` : ''}${who ? ` · moved by ${who}` : ''}`}>
      {positionLabel(pos)}
    </span>
  );
}
