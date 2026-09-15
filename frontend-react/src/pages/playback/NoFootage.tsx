/**
 * NoFootage.tsx — what a playback tile shows when there is nothing to play.
 *
 * A gap in recording is a NORMAL state in a VMS (the camera was offline, the
 * schedule had it off, retention aged the footage out), so it is presented
 * calmly and, above all, usefully: an operator who lands in a gap wants to know
 * where the footage actually IS, and to get there in one click. The NVR's 404
 * already tells us the camera's recorded range, so we surface it and offer to
 * jump — never a dead end, never a raw error blob.
 *
 * Only a genuine failure (5xx, network, permission) is styled as an error.
 */
import type { ReactNode } from 'react';

export type TileState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  /** In a gap, or before/after everything this camera recorded. */
  | { kind: 'gap'; earliest?: number; latest?: number }
  /** The camera has never recorded anything. */
  | { kind: 'none' }
  | { kind: 'error'; message: string };

const clock = (ms: number) =>
  new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const day = (ms: number) => new Date(ms).toLocaleDateString([], { day: '2-digit', month: 'short' });

/** "13:41 – 13:47" same day, else "14 Jul 13:41 – 15 Jul 09:02". */
function humanRange(a: number, b: number): string {
  const sameDay = new Date(a).toDateString() === new Date(b).toDateString();
  return sameDay
    ? `${clock(a)} – ${clock(b)}`
    : `${day(a)} ${clock(a)} – ${day(b)} ${clock(b)}`;
}

function Frame({ icon, title, sub, action }: {
  icon: ReactNode; title: string; sub?: ReactNode; action?: ReactNode;
}) {
  return (
    <div style={{
      position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
      alignItems: 'center', justifyContent: 'center', gap: 4, padding: 16,
      textAlign: 'center', background: '#0a0e13',
      // Never let a long string blow out the tile the way the raw JSON did.
      overflow: 'hidden',
    }}>
      <div style={{ fontSize: 22, lineHeight: 1, color: '#3d4f61', marginBottom: 2 }}>{icon}</div>
      <div style={{ fontSize: 13, fontWeight: 600, color: '#8fa3b8' }}>{title}</div>
      {sub && (
        <div style={{
          fontSize: 11, color: '#5b7186', maxWidth: '100%',
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>{sub}</div>
      )}
      {action}
    </div>
  );
}

const jumpBtn: React.CSSProperties = {
  marginTop: 8, padding: '5px 11px', fontSize: 11.5, fontWeight: 500,
  borderRadius: 6, border: '1px solid #2c3c47', background: 'transparent',
  color: '#cbd5e1', cursor: 'pointer',
};

export function NoFootage({ state, onJump, onRetry }: {
  state: TileState;
  /**
   * Seek to the nearest instant this camera has footage. The parent decides
   * WHICH edge — it knows where the playhead currently sits — and, with sync
   * lock on, moves every tile there.
   */
  onJump?: (range: { earliest: number; latest: number }) => void;
  onRetry?: () => void;
}) {
  if (state.kind === 'loading') {
    return <Frame icon={<span className="spinner" />} title="Loading…" />;
  }

  if (state.kind === 'idle') {
    return <Frame icon="⏱" title="Ready" sub="Pick a start time and press Load" />;
  }

  if (state.kind === 'none') {
    return (
      <Frame
        icon="⃠"
        title="No footage recorded"
        sub="This camera has nothing on the NVR yet"
      />
    );
  }

  if (state.kind === 'error') {
    return (
      <Frame
        icon="⚠"
        title="Couldn’t load footage"
        sub={state.message}
        action={onRetry && <button style={jumpBtn} onClick={onRetry}>Retry</button>}
      />
    );
  }

  // gap — the common, unalarming case.
  const { earliest, latest } = state;
  const known = earliest != null && latest != null;
  return (
    <Frame
      icon="⃠"
      title="No recording at this time"
      sub={known ? `Footage available ${humanRange(earliest!, latest!)}` : 'Gap in this camera’s recording'}
      action={known && onJump && (
        <button style={jumpBtn} title="Moves every tile — sync lock is on"
                onClick={() => onJump({ earliest: earliest!, latest: latest! })}>
          Jump to nearest footage
        </button>
      )}
    />
  );
}
