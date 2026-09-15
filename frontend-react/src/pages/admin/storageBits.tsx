/**
 * storageBits.tsx — the small pieces StorageTab is built from: the shapes the
 * NVR reports, the split (hot/cold) usage maths, and the tiny presentational
 * components.
 *
 * Split out so the page itself reads as the screen it is — disk summary, the
 * per-camera table, and the three admin modals — rather than 130 lines of
 * scaffolding first.
 */
import type { ReactNode } from 'react';

export interface NvrDisk {
  path: string;
  used_pct: number;
  used_gb: number;
  free_gb: number;
  total_gb: number;
  state: string;
  /** Set (e.g. "wsl2") when the sizes above are a dynamically expanding VIRTUAL
   *  disk rather than real host space — Docker Desktop on Windows backs
   *  /data/nvr with a ~1 TB-max vhdx living on a much smaller drive. Everything
   *  on this screen then overstates capacity, and because used_pct is measured
   *  against the virtual total it cannot reach the warning thresholds before
   *  the host drive is full. Null on a normal volume. */
  virtual_backing?: string | null;
}

export interface NvrHealth { status: string; disk: NvrDisk }

/** [earliest_start, latest_end] epoch seconds for a footage tier. */
export type EpochRange = [number, number];

export interface PerCameraUsage {
  total_bytes?: number; size_bytes?: number; bytes?: number;
  normal_bytes?: number; cold_bytes?: number;   // full-quality vs groomed
  normal_range?: EpochRange | null; cold_range?: EpochRange | null;
}

/** One state for the whole storage situation, plus why. `cap_binding` is the
 *  half that had no surface at all: the cap deleting footage on schedule is not
 *  a fault, it just means retention is shorter than anyone configured. */
export interface StoragePressure {
  state: 'ok' | 'warning' | 'critical';
  reasons: string[];
  disk_used_pct: number | null;
  cap_usage_pct: number | null;
  cap_binding: boolean;
  effective_retention_days: number | null;
  configured_retention_days: number | null;
}

/** Live probe of the volume the footage sits on, from GET /nvr/storage. */
export interface StorageDisk {
  path: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  reserve_bytes: number;
  reserve_pct: number;
  /** Same meaning as on NvrDisk: non-null when free_bytes describes a
   *  dynamically expanding virtual disk. The cap ceiling is derived from
   *  free_bytes, so when this is set the advertised maximum is an upper bound
   *  the host may not be able to honour. */
  virtual_backing?: string | null;
}

export interface NvrStorage {
  total_gb: number;
  limit_gb: number | null;
  usage_pct: number | null;
  /**
   * Largest cap the volume can honour: free space + footage already recorded,
   * less the reserve. Null when the NVR could not measure the disk, in which
   * case the UI falls back to the absolute bound — same fail-open rule the API
   * applies, so the two never disagree about what is settable.
   */
  max_limit_bytes?: number | null;
  max_limit_gb?: number | null;
  disk?: StorageDisk | null;
  /**
   * Storage pressure, evaluated by the NVR (storage/alerts.py). Null when
   * alerting is not wired — an older NVR, so the UI simply shows nothing rather
   * than inventing a state it cannot know.
   */
  pressure?: StoragePressure | null;
  normal_bytes?: number;
  cold_bytes?: number;
  normal_range?: EpochRange | null;
  cold_range?: EpochRange | null;
  groom_after_days_default?: number;
  per_camera: Record<string, PerCameraUsage>;
}

export interface PurgeResult { segments_deleted: number; bytes_freed: number }

export const gb = (bytes: number) => `${(bytes / 1024 ** 3).toFixed(2)} GB`;

/** Operator-facing size: TB above a terabyte, GB below. Mirrors the NVR's
 *  capacity.humanize so a hint and the server's rejection read the same. */
export const humanSize = (bytes: number): string =>
  bytes >= 1024 ** 4 ? `${(bytes / 1024 ** 4).toFixed(2)} TB` : gb(bytes);

export const spanDays = (r?: EpochRange | null): number | null =>
  r ? (r[1] - r[0]) / 86400 : null;

export const fmtDays = (d: number | null): string =>
  d == null ? '—' : d >= 9.95 ? `${Math.round(d)}` : d.toFixed(1);

/** Overall footage range = union of the hot and cold tier ranges. */
export function totalRange(u?: { normal_range?: EpochRange | null; cold_range?: EpochRange | null } | null): EpochRange | null {
  const rs = [u?.normal_range, u?.cold_range].filter((r): r is EpochRange => r != null);
  if (!rs.length) return null;
  return [Math.min(...rs.map(r => r[0])), Math.max(...rs.map(r => r[1]))];
}

/** "18 days — full quality 7.2 d · cold 11 d", tier split only if cold exists. */
export function daysLabel(u?: { normal_range?: EpochRange | null; cold_range?: EpochRange | null } | null): string | null {
  const total = spanDays(totalRange(u));
  if (total == null) return null;
  const base = `${fmtDays(total)} day${total >= 1.95 ? 's' : ''}`;
  if (!u?.cold_range) return base;
  return `${base} — full quality ${fmtDays(spanDays(u.normal_range))} d · cold ${fmtDays(spanDays(u.cold_range))} d`;
}

/** Full-quality (hot, orange) vs groomed keyframe-only (cold, slate) split. */
export function SplitBar({ normal, cold, width = 150 }: { normal: number; cold: number; width?: number }) {
  const total = normal + cold || 1;
  return (
    <span className="meter-track" style={{ width, height: 8, display: 'inline-flex' }}
          title={`Full quality ${gb(normal)} · Cold ${gb(cold)}`}>
      <span style={{ width: `${(normal / total) * 100}%`, background: 'var(--accent)', height: '100%' }} />
      <span style={{ width: `${(cold / total) * 100}%`, background: 'var(--tl-footage)', height: '100%' }} />
    </span>
  );
}

/** Per-camera hot/cold, tolerant of an older NVR that doesn't send the split. */
export function splitOf(u: PerCameraUsage | undefined, total: number): { normal: number; cold: number } {
  if (u?.normal_bytes != null || u?.cold_bytes != null) {
    return { normal: u.normal_bytes ?? 0, cold: u.cold_bytes ?? 0 };
  }
  return { normal: total, cold: 0 };   // no grooming data → treat all as full quality
}

/** Subtle icon-only action button (edit affordances) — inherits currentColor. */
export function IconButton({ label, onClick, disabled, children }:
  { label: string; onClick: () => void; disabled?: boolean; children: ReactNode }) {
  return (
    <button type="button" title={label} aria-label={label} onClick={onClick} disabled={disabled}
      style={{
        display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
        padding: 4, border: 'none', background: 'transparent', borderRadius: 6,
        color: 'var(--muted)', cursor: disabled ? 'default' : 'pointer', lineHeight: 0,
      }}
      onMouseEnter={e => { if (!disabled) e.currentTarget.style.color = 'var(--text)'; }}
      onMouseLeave={e => { e.currentTarget.style.color = 'var(--muted)'; }}>
      {children}
    </button>
  );
}

export const PencilIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
  </svg>
);

export const TuneIcon = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" />
    <line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" />
    <line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" />
    <line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" />
  </svg>
);

/** One KPI cell — small uppercase label (optional color dot) over a mono value. */
export function Stat({ label, value, dot }: { label: string; value: string; dot?: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 10, letterSpacing: '.07em', textTransform: 'uppercase', color: 'var(--muted)',
                    display: 'flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}>
        {dot && <span style={{ width: 7, height: 7, borderRadius: '50%', background: dot, flexShrink: 0 }} />}
        {label}
      </div>
      <div style={{ fontFamily: 'var(--mono)', fontSize: 13.5, color: 'var(--text)', marginTop: 4,
                    overflow: 'hidden', textOverflow: 'ellipsis' }}>{value}</div>
    </div>
  );
}

