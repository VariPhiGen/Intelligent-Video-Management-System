/**
 * onvif.tsx — types + the shared helpers for the ONVIF device editors, ported
 * from the legacy #stream-modal (stRange / imgSlider / imgSel / csFail).
 *
 * The three behaviours the legacy forms rely on, kept as-is:
 *   • stRange  — the camera's own min/max is shown IN THE LABEL, and is blank
 *     when the camera reported nothing.
 *   • disabled ≠ zero — a null current value means the camera doesn't expose
 *     that control, so the slider/input is disabled rather than showing 0; an
 *     empty modes list means the select is omitted entirely.
 *   • csFail   — a credentials error is not shown as an error. It is the one
 *     thing the operator can fix, so it deep-links to Maintenance.
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { apiFetch } from '@/lib/api';

// ── Types (every scalar is getattr-defensive server-side → all nullable) ─────

export interface Range { min: number | null; max: number | null }
export interface Res { width: number; height: number }

/** Encoder options come from either the Media2 or the ver10 ONVIF path, and the
 *  two carry different keys — on ver10 the codec block can be missing entirely,
 *  so these are ABSENT, not null. Optional-chain everything. */
export interface EncoderOptions {
  codec?: string | null;
  resolutions?: Res[];
  fps_range?: Range | null;
  gov_length_range?: Range | null;
  bitrate_kbps_range?: Range | null;
  quality_range?: Range | null;
  h264_profiles?: string[];
}

export interface EncoderProfile {
  profile_token: string | null;
  profile_name: string;
  /** The PUT keys off THIS, not profile_token. */
  config_token: string | null;
  config_name: string | null;
  encoding: string;
  width: number | null;
  height: number | null;
  quality: number | null;
  fps: number | null;
  bitrate_kbps: number | null;
  gov_length: number | null;
  h264_profile: string | null;
  options: EncoderOptions | null;
}

export interface ImagingOptions {
  brightness_range?: Range | null;
  contrast_range?: Range | null;
  color_saturation_range?: Range | null;
  sharpness_range?: Range | null;
  ir_cut_filter_modes?: string[];
  wdr?: { modes?: string[] } | null;
  blc?: { modes?: string[] } | null;
  exposure?: { modes?: string[] } | null;
  white_balance?: { modes?: string[] } | null;
  focus_modes?: string[];
}

export interface ImagingSource {
  source_token: string;
  brightness: number | null;
  contrast: number | null;
  color_saturation: number | null;
  sharpness: number | null;
  ir_cut_filter: string | null;
  /** GET nests these; the PUT body flattens them (wdr_mode, blc_mode, …). */
  wdr: { mode: string | null } | null;
  blc: { mode: string | null } | null;
  exposure: { mode: string | null } | null;
  white_balance: { mode: string | null } | null;
  focus_mode: string | null;
  options: ImagingOptions | null;
}

export interface Osd {
  token: string | null;
  type: string | null;       // "Text" | "Image"
  position: string | null;
  text_type: string | null;  // "Plain" | "Date" | "Time" | "DateAndTime"
  text: string | null;
  date_format: string | null;
  time_format: string | null;
}

export const OSD_POSITIONS = [
  'UpperLeft', 'UpperRight', 'LowerLeft', 'LowerRight', 'Center', 'Custom',
];

// ── Shared helpers (legacy stRange / imgSlider / imgSel / csFail) ────────────

/** legacy stRange — "1–25", or '' when the camera reported no bounds. */
export function rangeHint(r?: Range | null, unit = ''): string {
  if (!r || (r.min == null && r.max == null)) return '';
  return `${r.min ?? '?'}–${r.max ?? '?'}${unit}`;
}

export function Label({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <label style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
      <span>{children}</span>
      {hint && (
        <span style={{ fontWeight: 400, textTransform: 'none', letterSpacing: 0,
                       fontFamily: 'var(--mono)', fontSize: 10, color: 'var(--dim)' }}>
          {hint}
        </span>
      )}
    </label>
  );
}

/** legacy imgSlider — disabled when the camera doesn't report the value. */
export function Slider({ label, value, range, onChange }: {
  label: string; value: number | null; range?: Range | null; onChange: (v: number) => void;
}) {
  const disabled = value == null;
  return (
    <div className="form-group">
      <Label hint={rangeHint(range)}>{label}</Label>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input type="range" style={{ flex: 1, width: 'auto', padding: 0 }}
               min={range?.min ?? 0} max={range?.max ?? 100} step="any"
               value={value ?? 0} disabled={disabled}
               title={disabled ? 'Not exposed by this camera' : undefined}
               onChange={e => onChange(Number(e.target.value))} />
        <span style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)',
                       minWidth: 34, textAlign: 'right' }}>
          {disabled ? '—' : Number(value).toFixed(0)}
        </span>
      </div>
    </div>
  );
}

/** legacy imgSel — renders NOTHING when the camera reports no modes. Prepends
 *  the current value if the camera left it out of its own list (they do). */
export function Sel({ label, value, modes, onChange }: {
  label: string; value: string | null; modes?: string[]; onChange: (v: string) => void;
}) {
  if (!modes || !modes.length) return null;
  const list = value && !modes.includes(value) ? [value, ...modes] : modes;
  return (
    <div className="form-group">
      <Label>{label}</Label>
      <select value={value ?? ''} onChange={e => onChange(e.target.value)}>
        {!value && <option value="">—</option>}
        {list.map(m => <option key={m} value={m}>{m}</option>)}
      </select>
    </div>
  );
}

/** legacy csFail — credentials errors deep-link to Maintenance instead of
 *  dumping a red string the operator can do nothing with. */
export function ErrorState({ message, onOpenMaintenance }: {
  message: string; onOpenMaintenance?: () => void;
}) {
  const cred = /credential|ONVIF connection details/i.test(message || '');
  if (cred && onOpenMaintenance) {
    return (
      <div className="empty" style={{ padding: 20, flexDirection: 'column', gap: 10 }}>
        <div>{message}</div>
        <button className="btn-primary btn-sm" onClick={onOpenMaintenance}>
          Open Maintenance → enter credentials
        </button>
      </div>
    );
  }
  return <div style={{ color: 'var(--red)', fontSize: 12.5, padding: '10px 0' }}>{message}</div>;
}

/** Fetch on open. Talking to a camera over ONVIF is slow — hence the spinner. */
export function useOnvif<T>(path: string) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setErr('');
    try { setData(await apiFetch<T>(path)); }
    catch (e: any) { setErr(e.message); setData(null); }
    finally { setLoading(false); }
  }, [path]);

  useEffect(() => { load(); }, [load]);
  return { data, setData, err, setErr, loading, reload: load };
}

export function Loading() {
  return (
    <div className="empty" style={{ padding: 24, gap: 8 }}>
      <span className="spinner" /> Reading from the camera…
    </div>
  );
}
