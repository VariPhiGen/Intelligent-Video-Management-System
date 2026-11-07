/**
 * Filter chips for a camera list (lib/cameraFilters.ts). Counts are over the
 * list as given, before these filters; a chip with nothing to show is left
 * out, and a group with only one value is too (a "Maker" choice of one maker
 * filters nothing). Click a chip to filter, click it again to clear.
 */
import type { ReactNode } from 'react';
import { facets, isEmpty, STATUS_LABEL, TYPE_LABEL, type CameraFilter, type CamStatus, type CamType } from '@/lib/cameraFilters';
import type { Camera } from '@/lib/types';

type Group = 'status' | 'type' | 'maker' | 'recording' | 'attention';

const DOT: Record<CamStatus, string> = { online: 'var(--green)', offline: 'var(--red)', disabled: 'var(--dim)' };

export function CameraFilterBar({ cameras, value, onChange, groups = ['status', 'type', 'maker', 'recording', 'attention'], compact = false }: {
  cameras: Camera[];
  value: CameraFilter;
  onChange: (f: CameraFilter) => void;
  groups?: Group[];
  /** Narrow rails: chips wrap tighter and group labels stack. */
  compact?: boolean;
}) {
  const fc = facets(cameras);
  const set = <K extends keyof CameraFilter>(k: K, v: CameraFilter[K]) =>
    onChange({ ...value, [k]: value[k] === v ? undefined : v });
  const chip = (key: string, label: string, n: number, active: boolean, onClick: () => void, dot?: string, title?: string) => (
    <button key={key} type="button" className={`chip cf-chip${active ? ' active' : ''}`} aria-pressed={active}
            onClick={onClick} title={title}>
      {dot && <span className="cdot" style={{ background: dot }} />}
      {label}<span className="cf-n">{n}</span>
    </button>
  );
  const rows: { label: string; chips: ReactNode[] }[] = [];

  if (groups.includes('status')) {
    const vals = (Object.keys(STATUS_LABEL) as CamStatus[]).filter(s => fc.status[s] > 0);
    if (vals.length > 1 || value.status) {
      rows.push({ label: 'Status', chips: vals.map(s => chip(`s-${s}`, STATUS_LABEL[s], fc.status[s], value.status === s,
        () => set('status', s), DOT[s])) });
    }
  }
  if (groups.includes('type')) {
    const vals = (Object.keys(TYPE_LABEL) as CamType[]).filter(t => fc.type[t] > 0);
    if (vals.length > 1 || value.type) {
      rows.push({ label: 'Type', chips: vals.map(t => chip(`t-${t}`, TYPE_LABEL[t], fc.type[t], value.type === t,
        () => set('type', t), undefined,
        t === 'unchecked' ? 'PTZ never checked — usually because the ONVIF login has not worked yet' : undefined)) });
    }
  }
  if (groups.includes('maker')) {
    const keys = Object.keys(fc.maker).sort((a, b) => fc.maker[b].n - fc.maker[a].n || a.localeCompare(b));
    if (keys.length > 1 || value.maker) {
      rows.push({ label: 'Maker', chips: keys.map(k => chip(`m-${k}`, fc.maker[k].label || k, fc.maker[k].n, value.maker === k,
        () => set('maker', k))) });
    }
  }
  if (groups.includes('recording') && ((fc.recording.on && fc.recording.off) || value.recording)) {
    rows.push({ label: 'Recording', chips: [
      chip('r-on', 'Recording', fc.recording.on, value.recording === 'on', () => set('recording', 'on')),
      chip('r-off', 'Not recording', fc.recording.off, value.recording === 'off', () => set('recording', 'off')),
    ] });
  }
  if (groups.includes('attention') && (fc.attention > 0 || value.attention)) {
    rows.push({ label: 'Attention', chips: [
      chip('a', 'No ONVIF login', fc.attention, !!value.attention, () => set('attention', value.attention ? undefined : true),
        'var(--yellow)', 'Has an address but no working ONVIF login — PTZ, stream settings and maintenance can\'t work. Fix it in Configuration → Maintenance.'),
    ] });
  }
  if (!rows.length) return null;

  return (
    <div className={`cf-bar${compact ? ' cf-compact' : ''}`}>
      {rows.map(r => (
        <div key={r.label} className="cf-row">
          <span className="cf-label">{r.label}</span>
          <div className="cf-chips">{r.chips}</div>
        </div>
      ))}
      {!isEmpty(value) && (
        <button type="button" className="btn-subtle btn-sm cf-clear" onClick={() => onChange({})}>Clear filters</button>
      )}
    </div>
  );
}
