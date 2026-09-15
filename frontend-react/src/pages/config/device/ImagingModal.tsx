/**
 * ImagingModal.tsx — image settings, ported from the legacy csLoadImage /
 * imgRender / imgApply. One card per video source (almost always one).
 *
 * Unlike the encoder, these apply live — no stream restart, no relay reconnect.
 */
import { useState } from 'react';
import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import type { Camera } from '@/lib/types';
import {
  ErrorState, Loading, Sel, Slider, useOnvif, type ImagingSource,
} from './onvif';

interface ImagingResp { sources: ImagingSource[] }

/** Sliders the camera reported a value for; a null current value means the
 *  control doesn't exist on this camera and must never be sent. */
const SLIDERS = [
  ['brightness', 'Brightness', 'brightness_range'],
  ['contrast', 'Contrast', 'contrast_range'],
  ['color_saturation', 'Saturation', 'color_saturation_range'],
  ['sharpness', 'Sharpness', 'sharpness_range'],
] as const;

function SourceCard({ camId, source, onSaved, onError }: {
  camId: string;
  source: ImagingSource;
  onSaved: (s: ImagingSource) => void;
  onError: (m: string) => void;
}) {
  const toast = useToast();
  const s = source;
  const o = s.options;
  const [d, setD] = useState<Record<string, number | string>>({});
  const [busy, setBusy] = useState(false);

  const numOf = (k: string, was: number | null) =>
    (d[k] !== undefined ? Number(d[k]) : was);
  const strOf = (k: string, was: string | null) =>
    (d[k] !== undefined ? String(d[k]) : was);
  const set = (k: string, v: number | string) => setD(x => ({ ...x, [k]: v }));

  async function apply() {
    const changes: Record<string, number | string> = {};

    for (const [key] of SLIDERS) {
      const was = s[key] as number | null;
      const now = numOf(key, was);
      // Float values against int ranges — compare with an epsilon, and never
      // send a control the camera never reported.
      if (was != null && now != null && Math.abs(now - was) > 0.01) changes[key] = now;
    }

    // GET nests these (wdr.mode); the PUT body flattens them (wdr_mode).
    const modes: [string, string | null | undefined][] = [
      ['ir_cut_filter', strOf('ir_cut_filter', s.ir_cut_filter)],
      ['wdr_mode', strOf('wdr_mode', s.wdr?.mode ?? null)],
      ['blc_mode', strOf('blc_mode', s.blc?.mode ?? null)],
      ['exposure_mode', strOf('exposure_mode', s.exposure?.mode ?? null)],
      ['wb_mode', strOf('wb_mode', s.white_balance?.mode ?? null)],
      ['focus_mode', strOf('focus_mode', s.focus_mode)],
    ];
    const wasOf: Record<string, string | null> = {
      ir_cut_filter: s.ir_cut_filter,
      wdr_mode: s.wdr?.mode ?? null,
      blc_mode: s.blc?.mode ?? null,
      exposure_mode: s.exposure?.mode ?? null,
      wb_mode: s.white_balance?.mode ?? null,
      focus_mode: s.focus_mode,
    };
    for (const [field, now] of modes) {
      if (now && now !== wasOf[field]) changes[field] = now;
    }

    if (!Object.keys(changes).length) { toast('Nothing changed'); return; }
    setBusy(true);
    try {
      const resp = await apiFetch<{ source: ImagingSource | null }>(
        `/cameras/${camId}/imaging/${encodeURIComponent(s.source_token)}`,
        { method: 'PUT', body: JSON.stringify(changes) },
      );
      if (resp.source) onSaved(resp.source);
      setD({});
      toast('Image settings applied');
    } catch (e: any) {
      onError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', marginBottom: 10 }}>
        <b style={{ fontSize: 13.5 }}>Image</b>
        <code style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--dim)' }}>{s.source_token}</code>
      </div>

      <div className="form-grid">
        {SLIDERS.map(([key, label, rangeKey]) => (
          <Slider key={key} label={label}
                  value={numOf(key, s[key] as number | null)}
                  range={o?.[rangeKey]}
                  onChange={v => set(key, v)} />
        ))}
      </div>

      <div className="form-grid" style={{ marginTop: 10 }}>
        <Sel label="Day / Night (IR)" value={strOf('ir_cut_filter', s.ir_cut_filter)}
             modes={o?.ir_cut_filter_modes} onChange={v => set('ir_cut_filter', v)} />
        <Sel label="WDR" value={strOf('wdr_mode', s.wdr?.mode ?? null)}
             modes={o?.wdr?.modes} onChange={v => set('wdr_mode', v)} />
        <Sel label="Backlight comp." value={strOf('blc_mode', s.blc?.mode ?? null)}
             modes={o?.blc?.modes} onChange={v => set('blc_mode', v)} />
        <Sel label="Exposure" value={strOf('exposure_mode', s.exposure?.mode ?? null)}
             modes={o?.exposure?.modes} onChange={v => set('exposure_mode', v)} />
        <Sel label="White balance" value={strOf('wb_mode', s.white_balance?.mode ?? null)}
             modes={o?.white_balance?.modes} onChange={v => set('wb_mode', v)} />
        <Sel label="Focus" value={strOf('focus_mode', s.focus_mode)}
             modes={o?.focus_modes} onChange={v => set('focus_mode', v)} />
      </div>

      <div className="form-actions">
        <button className="btn-primary btn-sm" onClick={apply} disabled={busy}>
          {busy ? <span className="spinner" /> : 'Apply'}
        </button>
      </div>

      <div className="d-hint" style={{ marginTop: 8 }}>
        Changes apply live — no stream restart. Disabled sliders and missing selects are settings this camera
        does not report. The form refreshes with what the camera actually kept.
      </div>
    </div>
  );
}

export function ImagingModal({ camera, onClose, onOpenMaintenance }: {
  camera: Camera;
  onClose: () => void;
  onOpenMaintenance: () => void;
}) {
  const { data, setData, err, setErr, loading } = useOnvif<ImagingResp>(`/cameras/${camera.id}/imaging`);
  const sources = data?.sources ?? [];

  return (
    <Modal open title={`Image — ${camera.name}`} onClose={onClose} width={620}>
      {loading && <Loading />}
      {!loading && err && <ErrorState message={err} onOpenMaintenance={onOpenMaintenance} />}
      {!loading && !err && !sources.length && (
        <div className="empty">This camera reports no imaging sources.</div>
      )}
      {!loading && !err && sources.map((s, i) => (
        <SourceCard key={s.source_token || i} camId={camera.id} source={s}
          onError={setErr}
          onSaved={ns => setData(d => d && {
            ...d, sources: d.sources.map((x, j) => (j === i ? ns : x)),
          })} />
      ))}
    </Modal>
  );
}
