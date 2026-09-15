/**
 * EncoderModal.tsx — stream settings, ported from the legacy csLoadStream /
 * stRender / stApply. One card per encoder profile (Main / Sub).
 *
 * Applying here force-reconnects the relay server-side (most cameras restart the
 * stream to change encoding), which imaging does NOT — hence the footer warning.
 */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import type { Camera } from '@/lib/types';
import {
  ErrorState, Label, Loading, rangeHint, useOnvif,
  type EncoderProfile, type Res,
} from './onvif';

interface EncoderResp { profiles: EncoderProfile[] }
type Draft = Record<string, string>;

const resKey = (r: Res) => `${r.width}x${r.height}`;

function ProfileCard({ camId, profile, onSaved, onError }: {
  camId: string;
  profile: EncoderProfile;
  onSaved: (p: EncoderProfile) => void;
  onError: (m: string) => void;
}) {
  const toast = useToast();
  const p = profile;
  const o = p.options;
  const [d, setD] = useState<Draft>({});
  const [busy, setBusy] = useState(false);

  // The draft holds only what the operator touched; everything else reads
  // straight from the camera's last read-back.
  const cur = (k: string, fallback: number | string | null) =>
    d[k] !== undefined ? d[k] : (fallback == null ? '' : String(fallback));
  const set = (k: string, v: string) => setD(s => ({ ...s, [k]: v }));

  const resList = o?.resolutions ?? [];
  const curRes = p.width && p.height ? `${p.width}x${p.height}` : '';

  async function apply() {
    const changes: Record<string, number> = {};

    // width+height only ever move as a pair — the API 422s on a lone one.
    const r = cur('res', curRes);
    if (/^\d+x\d+$/i.test(r) && r !== curRes) {
      const [w, h] = r.split('x').map(Number);
      changes.width = w;
      changes.height = h;
    }
    const num = (k: string, field: string, was: number | null) => {
      const raw = cur(k, was);
      if (raw === '') return;
      const v = Number(raw);
      if (!Number.isNaN(v) && v !== was) changes[field] = v;
    };
    num('fps', 'fps', p.fps);
    num('br', 'bitrate_kbps', p.bitrate_kbps);
    if (p.gov_length != null) num('gop', 'gov_length', p.gov_length);
    num('q', 'quality', p.quality);

    if (!Object.keys(changes).length) { toast('Nothing changed'); return; }
    setBusy(true);
    try {
      const resp = await apiFetch<{ profile: EncoderProfile | null }>(
        `/cameras/${camId}/encoder/${encodeURIComponent(p.config_token || '')}`,
        { method: 'PUT', body: JSON.stringify(changes) },
      );
      // Cameras clamp silently — snap the form back to what it actually kept.
      if (resp.profile) onSaved(resp.profile);
      setD({});
      toast('Stream settings applied');
    } catch (e: any) {
      onError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
        <b style={{ fontSize: 13.5 }}>{p.profile_name}</b>
        <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
          {[p.encoding, p.h264_profile].filter(Boolean).join(' ')}
        </span>
        <code style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--dim)' }}>{p.config_token}</code>
      </div>

      <div className="form-grid">
        <div className="form-group">
          <Label>Resolution</Label>
          {resList.length ? (
            <select value={cur('res', curRes)} onChange={e => set('res', e.target.value)}>
              {/* Cameras routinely run at a resolution absent from their own
                  advertised list — keep it selectable rather than silently
                  rewriting it on the next apply. */}
              {curRes && !resList.some(r => resKey(r) === curRes) && (
                <option value={curRes}>{p.width} × {p.height} (current)</option>
              )}
              {resList.map(r => (
                <option key={resKey(r)} value={resKey(r)}>{r.width} × {r.height}</option>
              ))}
            </select>
          ) : (
            <input placeholder="1920x1080" value={cur('res', curRes)}
                   onChange={e => set('res', e.target.value)} />
          )}
        </div>

        <div className="form-group">
          <Label hint={rangeHint(o?.fps_range)}>FPS</Label>
          <input type="number" min={o?.fps_range?.min ?? undefined} max={o?.fps_range?.max ?? undefined}
                 value={cur('fps', p.fps)} onChange={e => set('fps', e.target.value)} />
        </div>

        <div className="form-group">
          <Label hint={rangeHint(o?.bitrate_kbps_range)}>Bitrate (kbps)</Label>
          <input type="number" value={cur('br', p.bitrate_kbps)}
                 onChange={e => set('br', e.target.value)} />
        </div>

        <div className="form-group">
          <Label hint={rangeHint(o?.gov_length_range)}>GOP length</Label>
          <input type="number" value={cur('gop', p.gov_length)}
                 disabled={p.gov_length == null}
                 title={p.gov_length == null ? 'Not exposed by this configuration' : undefined}
                 onChange={e => set('gop', e.target.value)} />
        </div>

        <div className="form-group">
          <Label hint={rangeHint(o?.quality_range)}>Quality</Label>
          <input type="number" step="any" value={cur('q', p.quality)}
                 onChange={e => set('q', e.target.value)} />
        </div>
      </div>

      <div className="form-actions">
        <button className="btn-primary btn-sm" onClick={apply} disabled={busy}>
          {busy ? <span className="spinner" /> : 'Apply'}
        </button>
      </div>
    </div>
  );
}

export function EncoderModal({ camera, onClose, onOpenMaintenance }: {
  camera: Camera;
  onClose: () => void;
  onOpenMaintenance: () => void;
}) {
  const { data, setData, err, setErr, loading } = useOnvif<EncoderResp>(`/cameras/${camera.id}/encoder`);
  const profiles = data?.profiles ?? [];
  const [active, setActive] = useState(0);

  // Keep the selection valid if the list shrinks (or arrives late).
  useEffect(() => {
    if (active >= profiles.length && profiles.length) setActive(0);
  }, [profiles.length, active]);

  const p = profiles[active];

  return (
    <Modal open title={`Stream settings — ${camera.name}`} onClose={onClose} width={560}>
      {loading && <Loading />}
      {!loading && err && <ErrorState message={err} onOpenMaintenance={onOpenMaintenance} />}
      {!loading && !err && !profiles.length && (
        <div className="empty">This camera reports no encoder profiles.</div>
      )}

      {/* One stream per view. Cameras expose a main + one or more sub streams;
          a switcher shows the chosen one full-size instead of stacking them all
          into a page-tall scroll. Only shown when there's more than one. */}
      {!loading && !err && profiles.length > 1 && (
        <div className="seg" role="tablist" aria-label="Encoder profiles">
          {profiles.map((pf, i) => (
            <button key={pf.config_token || i} role="tab" aria-selected={i === active}
              className={`seg-item${i === active ? ' active' : ''}`} onClick={() => setActive(i)}>
              {pf.profile_name || `Stream ${i + 1}`}
              <span className="seg-sub">{pf.encoding || '—'}</span>
            </button>
          ))}
        </div>
      )}

      {!loading && !err && p && (
        <ProfileCard key={p.config_token || active} camId={camera.id} profile={p}
          onError={setErr}
          onSaved={np => setData(d => d && {
            ...d, profiles: d.profiles.map((x, j) => (j === active ? np : x)),
          })} />
      )}

      {!loading && !err && !!profiles.length && (
        <div className="d-hint">
          Settings are written to the camera over ONVIF. Most cameras briefly restart the stream to apply
          them — the relay reconnects automatically. Out-of-range values are clamped by the camera; the form
          refreshes with what it actually kept.
        </div>
      )}
    </Modal>
  );
}
