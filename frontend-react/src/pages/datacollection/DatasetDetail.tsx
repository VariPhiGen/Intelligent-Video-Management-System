/**
 * DatasetDetail — one dataset: its recipe, its run state, and its frames.
 *
 * Collection is a BACKGROUND JOB on the server (202, then the row moves
 * draft → collecting → ready/failed), so this polls while a run is in flight
 * and renders real progress from `frames_collected`. It is not a spinner
 * waiting on a request: an operator can navigate away and come back, and a run
 * that died with its worker is reaped into `failed` rather than spinning
 * forever.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { useToast } from '@/components/Toast';
import { AuthImg, ImageLightbox } from '@/components/AuthImage';
import { fmtBytes } from '@/lib/format';
import { fmtInterval, statusBadge, type Dataset, type Frame, type FrameManifest } from './types';

const POLL_MS = 2000;

export function DatasetDetail({ dataset, onChanged, onEdit, onImport, onDelete, onBack }: {
  dataset: Dataset;
  onChanged: (d: Dataset) => void;
  onEdit: () => void;
  onImport: () => void;
  onDelete: () => void;
  onBack: () => void;
}) {
  const toast = useToast();
  const [frames, setFrames] = useState<Frame[]>([]);
  const [lightbox, setLightbox] = useState<number | null>(null);
  const [starting, setStarting] = useState(false);
  const alive = useRef(true);

  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const loadFrames = useCallback(async () => {
    if (dataset.status !== 'ready' || !dataset.frames_collected) { setFrames([]); return; }
    try {
      const m = await apiFetch<FrameManifest>(`/datasets/${dataset.id}/frames`);
      if (alive.current) setFrames(m.frames || []);
    } catch { /* the grid is not worth an error toast */ }
  }, [dataset.id, dataset.status, dataset.frames_collected]);

  useEffect(() => { loadFrames(); }, [loadFrames]);

  // Poll only while a run is actually in flight, and stop the moment it is not.
  useEffect(() => {
    if (dataset.status !== 'collecting') return;
    const timer = setInterval(async () => {
      try {
        const fresh = await apiFetch<Dataset>(`/datasets/${dataset.id}`);
        if (!alive.current) return;
        onChanged(fresh);
        if (fresh.status === 'ready') toast(`Collected ${fresh.frames_collected} frames`);
        if (fresh.status === 'failed') toast(fresh.build_error || 'Collection failed', 'err');
      } catch { /* transient: the next tick retries */ }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [dataset.id, dataset.status, onChanged, toast]);

  async function collect() {
    setStarting(true);
    try {
      const started = await apiFetch<Dataset>(`/datasets/${dataset.id}/collect`, { method: 'POST' });
      onChanged(started);
      toast('Collection started — this runs in the background');
    } catch (e: any) {
      toast(e?.message || 'Could not start collection', 'err');
    } finally {
      setStarting(false);
    }
  }

  const badge = statusBadge(dataset.status);
  const collecting = dataset.status === 'collecting';
  const pct = collecting && dataset.target_frames
    ? Math.min(100, Math.round((dataset.frames_collected / dataset.target_frames) * 100))
    : 0;

  return (
    <div className="fade">
      <div className="page-head">
        <div className="page-head-l">
          <button className="btn btn-sm btn-ghost" onClick={onBack}>← All datasets</button>
          <h2 style={{ margin: 0 }}>{dataset.name}</h2>
          <span className={`badge ${badge.cls}`}><span className="badge-dot" />{badge.label}</span>
        </div>
        <div className="page-head-actions">
          <button className="btn btn-sm" onClick={onEdit} disabled={collecting}>Edit</button>
          <button className="btn btn-sm btn-primary" onClick={collect} disabled={collecting || starting}>
            {collecting ? 'Collecting…' : starting ? 'Starting…'
              : dataset.frames_collected ? 'Re-collect' : 'Collect frames'}
          </button>
          <button className="btn btn-sm" onClick={onImport}
                  disabled={dataset.status !== 'ready' || !dataset.frames_collected}>
            Import…
          </button>
          <button className="btn btn-sm btn-quiet-danger" onClick={onDelete} disabled={collecting}>
            Delete
          </button>
        </div>
      </div>

      {dataset.description && <p className="subtitle">{dataset.description}</p>}

      {/* The recipe. The interval sits beside the frame count everywhere,
          because it — not the count — says how dense the training set is. */}
      <div className="stat-grid" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="stat-label">Camera</div>
          <div className="stat-val-text">{dataset.camera_slug}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Window</div>
          <div className="stat-val-text">
            {new Date(dataset.window_start).toLocaleString()}
            <div className="stat-sub">→ {new Date(dataset.window_end).toLocaleString()}</div>
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Frames</div>
          <div className="stat-val">{dataset.frames_collected || dataset.target_frames}</div>
          <div className="stat-sub">
            {dataset.frames_collected ? `of ${dataset.target_frames} ${dataset.model_id ? 'max' : 'requested'}` : dataset.model_id ? 'max kept' : 'requested'}
          </div>
        </div>
        {(dataset.ptz_preset_ids || []).length > 0 ? (
          <div className="stat">
            <div className="stat-label">At presets</div>
            <div className="stat-val-text">
              {(() => {
                const by: Record<string, number> = {};
                frames.forEach(f => { if (f.preset_name) by[f.preset_name] = (by[f.preset_name] || 0) + 1; });
                const names = Object.entries(by).map(([n, c]) => `${n} (${c})`);
                return names.length ? names.join(', ') : `${dataset.ptz_preset_ids.length} chosen`;
              })()}
            </div>
            <div className="stat-sub">frames only while the camera was at them — not the moves between</div>
          </div>
        ) : (
          <div className="stat">
            <div className="stat-label">Sampling interval</div>
            <div className="stat-val-text">{fmtInterval(dataset.interval_seconds)}</div>
            <div className="stat-sub">how far apart the images are</div>
          </div>
        )}
        {dataset.model_id && (
          <div className="stat">
            <div className="stat-label">Detection filter</div>
            <div className="stat-val-text">{dataset.target_classes.join(', ')}</div>
            <div className="stat-sub">
              {dataset.model_name || 'model'} · ≥{Math.round((dataset.min_confidence ?? 0.35) * 100)}%
              {dataset.frames_scanned > 0 &&
                ` · kept ${dataset.frames_collected} of ${dataset.frames_scanned} scanned`}
            </div>
          </div>
        )}
        {dataset.frames_missing > 0 && (
          <div className="stat">
            <div className="stat-label">Missing</div>
            <div className="stat-val">{dataset.frames_missing}</div>
            <div className="stat-sub">gaps in the recording</div>
          </div>
        )}
        {dataset.bytes_total != null && (
          <div className="stat">
            <div className="stat-label">Size</div>
            <div className="stat-val-text">{fmtBytes(dataset.bytes_total)}</div>
          </div>
        )}
      </div>

      {collecting && (
        <div className="panel" style={{ marginBottom: 16 }}>
          <div className="panel-body">
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
              <strong>Collecting frames…</strong>
              <span>{dataset.frames_collected} / {dataset.target_frames}</span>
            </div>
            <div style={{ height: 6, background: 'var(--track)', borderRadius: 999 }}>
              <div style={{ width: `${pct}%`, height: '100%', background: 'var(--accent)',
                            borderRadius: 999, transition: 'width 400ms var(--ease)' }} />
            </div>
            <div className="hint" style={{ marginTop: 8 }}>
              This runs on the server — you can leave this page and come back.
            </div>
          </div>
        </div>
      )}

      {dataset.status === 'failed' && dataset.build_error && (
        <div className="banner banner-crit" style={{ marginBottom: 16 }}>{dataset.build_error}</div>
      )}

      {(dataset.labelstudio_exported_at || dataset.s3_uploaded_at) && (
        <div className="kv" style={{ marginBottom: 16 }}>
          {dataset.labelstudio_exported_at && (
            <div className="kv-row">
              <span>Label Studio</span>
              <strong>
                {dataset.labelstudio_task_count} tasks in project{' '}
                {dataset.labelstudio_project_id} ·{' '}
                {new Date(dataset.labelstudio_exported_at).toLocaleString()}
              </strong>
            </div>
          )}
          {dataset.s3_uploaded_at && (
            <div className="kv-row">
              <span>S3</span>
              <strong>
                {dataset.s3_uploaded_count} objects at s3://{dataset.s3_bucket}/
                {dataset.s3_prefix || ''} ·{' '}
                {new Date(dataset.s3_uploaded_at).toLocaleString()}
              </strong>
            </div>
          )}
        </div>
      )}

      {/* The frame grid. Every image is Bearer-gated, so AuthImg fetches the
          bytes and hands the tag an object URL — a plain <img src> cannot
          carry the token. */}
      {frames.length > 0 && (
        <div className="panel">
          <div className="panel-head">
            <span className="panel-title">Frames</span>
            <span className="panel-sub">
              {frames.length} images · {fmtInterval(dataset.interval_seconds)}
            </span>
          </div>
          <div className="panel-body">
            <div style={{ display: 'grid', gap: 8,
                          gridTemplateColumns: 'repeat(auto-fill, minmax(148px, 1fr))' }}>
              {frames.map((f, i) => (
                <AuthImg
                  key={f.filename}
                  path={`/datasets/${dataset.id}/frames/${f.filename}`}
                  alt={f.timestamp}
                  title={`${new Date(f.timestamp).toLocaleString()}${f.preset_name ? ` · ${f.preset_name}` : ''}`}
                  onClick={() => setLightbox(i)}
                  style={{ width: '100%', aspectRatio: '16 / 9', objectFit: 'cover',
                           borderRadius: 6, border: '1px solid var(--border)' }}
                />
              ))}
            </div>
          </div>
        </div>
      )}

      <ImageLightbox
        path={lightbox !== null && frames[lightbox]
          ? `/datasets/${dataset.id}/frames/${frames[lightbox].filename}` : null}
        caption={lightbox !== null && frames[lightbox]
          ? `${new Date(frames[lightbox].timestamp).toLocaleString()}${frames[lightbox].preset_name ? ` · ${frames[lightbox].preset_name}` : ''} · ${lightbox + 1} of ${frames.length}`
          : undefined}
        onClose={() => setLightbox(null)}
        onPrev={lightbox !== null && lightbox > 0 ? () => setLightbox(lightbox - 1) : undefined}
        onNext={lightbox !== null && lightbox < frames.length - 1
          ? () => setLightbox(lightbox + 1) : undefined}
      />
    </div>
  );
}
