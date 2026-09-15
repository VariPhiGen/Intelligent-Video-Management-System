/** bits.tsx — small shared pieces for the Configuration page (legacy statusBadge / chip). */
import type { ReactNode } from 'react';

const STATUS_BADGE: Record<string, [string, string]> = {
  connected: ['badge-green', 'Live'],
  disconnected: ['badge-red', 'Offline'],
  error: ['badge-red', 'Error'],
  unknown: ['badge-yellow', 'Unknown'],
};

export function StatusBadge({ status, enabled }: { status: string; enabled: boolean }) {
  const [cls, label] = !enabled
    ? ['badge-gray', 'Disabled']
    : (STATUS_BADGE[status] || ['badge-gray', status]);
  return <span className={`badge ${cls}`}><span className="badge-dot" />{label}</span>;
}

/** Legacy cfgRender chip(label, value) — tiny uppercase label over a bold value. */
export function Chip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ minWidth: 96 }}>
      <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: '.08em', color: 'var(--dim)', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div style={{ fontSize: 13, fontWeight: 600, marginTop: 3 }}>{children}</div>
    </div>
  );
}
