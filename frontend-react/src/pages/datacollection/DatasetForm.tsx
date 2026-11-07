/**
 * DatasetForm — create or edit a dataset recipe.
 *
 * Two things this form does that a plain CRUD form would not, both because a
 * collection run is expensive (~1.3 s of NVR decode per frame):
 *
 *  • It asks /coverage as the camera or window changes and WARNS BEFORE the
 *    run, rather than letting an operator spend minutes extracting nothing
 *    from a window retention already groomed away.
 *  • It shows the sampling interval and an estimated duration live, so the
 *    consequences of raising the frame count are visible while typing it.
 */
import { useEffect, useState } from 'react';
import { apiFetch, apiUpload } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { dtLocal } from '@/lib/format';
import { useCameras } from '@/lib/cameras';
import type { Camera } from '@/lib/types';
import {
  fmtEstimate, fmtInterval, fmtSpan, type Coverage, type Dataset, type DetectionModel, type PtzTime,
} from './types';

function toIso(local: string): string {
  return new Date(local).toISOString();
}

export function DatasetForm({ open, existing, onClose, onSaved }: {
  open: boolean;
  existing: Dataset | null;
  onClose: () => void;
  onSaved: (d: Dataset) => void;
}) {
  const { cameras } = useCameras();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [slug, setSlug] = useState('');
  const [start, setStart] = useState('');
  const [end, setEnd] = useState('');
  const [target, setTarget] = useState(200);
  const [quality, setQuality] = useState(85);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Detection filter: keep only frames in which a chosen class is detected.
  const [models, setModels] = useState<DetectionModel[]>([]);
  const [modelId, setModelId] = useState('');
  const [classes, setClasses] = useState<string[]>([]);
  const [minConf, setMinConf] = useState(0.35);
  const [scanEvery, setScanEvery] = useState(2);
  // PTZ phase 4: only frames while the camera was confirmed at these presets ([] = the whole window).
  const [presetIds, setPresetIds] = useState<string[]>([]);
  const [ptzTime, setPtzTime] = useState<PtzTime | null>(null);
  const [uploading, setUploading] = useState(false);
  const [modelError, setModelError] = useState<string | null>(null);

  const model = models.find(m => m.id === modelId) || null;

  // The model list lives on the analytics service; a failure here must not
  // block plain (unfiltered) datasets, so it is shown inline, not as an error.
  useEffect(() => {
    if (!open) return;
    setModelError(null);
    apiFetch<{ models: DetectionModel[] }>('/datasets/models')
      .then(r => setModels(r.models))
      .catch((e: any) => { setModels([]); setModelError(e?.message || 'Models unavailable'); });
  }, [open]);

  async function uploadModel(file: File) {
    setUploading(true);
    setModelError(null);
    try {
      const form = new FormData();
      form.append('file', file);
      form.append('name', file.name.replace(/\.(pt|onnx)$/i, ''));
      const m = await apiUpload<DetectionModel>('/datasets/models', form);
      setModels(prev => [m, ...prev]);
      setModelId(m.id);
      setClasses([]);
    } catch (e: any) {
      setModelError(e?.message || 'Upload failed');
    } finally {
      setUploading(false);
    }
  }

  function toggleClass(c: string) {
    setClasses(prev => prev.includes(c) ? prev.filter(x => x !== c) : [...prev, c]);
  }

  // Seed the form each time it opens: editing loads the row, creating defaults
  // to the last two hours, which is the window an operator almost always wants.
  useEffect(() => {
    if (!open) return;
    setError(null);
    if (existing) {
      setName(existing.name);
      setDescription(existing.description || '');
      setSlug(existing.camera_slug);
      setStart(dtLocal(new Date(existing.window_start)));
      setEnd(dtLocal(new Date(existing.window_end)));
      setTarget(existing.target_frames);
      setQuality(existing.jpeg_quality);
      setModelId(existing.model_id || '');
      setClasses(existing.target_classes || []);
      setMinConf(existing.min_confidence ?? 0.35);
      setScanEvery(existing.scan_interval_seconds ?? 2);
      setPresetIds(existing.ptz_preset_ids || []);
    } else {
      const now = new Date();
      const then = new Date(now.getTime() - 2 * 3600 * 1000);
      setName('');
      setDescription('');
      setSlug('');
      setStart(dtLocal(then));
      setEnd(dtLocal(now));
      setTarget(200);
      setQuality(85);
      setModelId('');
      setClasses([]);
      setMinConf(0.35);
      setScanEvery(2);
      setPresetIds([]);
    }
  }, [open, existing]);

  // A PTZ camera: its presets and the time at each in the window (debounced like coverage). A preset it never
  // visited in the window gives no frames — the numbers are shown so that's seen before the run, not after.
  useEffect(() => {
    if (!open || !slug || !start || !end || new Date(start) >= new Date(end)) { setPtzTime(null); return; }
    let cancelled = false;
    const timer = setTimeout(() => {
      const q = new URLSearchParams({ camera_slug: slug, window_start: toIso(start), window_end: toIso(end) });
      apiFetch<PtzTime>(`/datasets/ptz-time?${q}`)
        .then(t => { if (!cancelled) setPtzTime(t); })
        .catch(() => { if (!cancelled) setPtzTime(null); });
    }, 400);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [open, slug, start, end]);

  // Coverage follows the camera and the window. Debounced because the window
  // fields fire on every keystroke and each check is an NVR round-trip.
  useEffect(() => {
    if (!open || !slug || !start || !end) { setCoverage(null); return; }
    if (new Date(start) >= new Date(end)) { setCoverage(null); return; }
    let cancelled = false;
    setChecking(true);
    const timer = setTimeout(() => {
      const q = new URLSearchParams({
        camera_slug: slug,
        window_start: toIso(start),
        window_end: toIso(end),
      });
      apiFetch<Coverage>(`/datasets/coverage?${q}`)
        .then(c => { if (!cancelled) setCoverage(c); })
        .catch(() => { if (!cancelled) setCoverage(null); })
        .finally(() => { if (!cancelled) setChecking(false); });
    }, 400);
    return () => { cancelled = true; clearTimeout(timer); setChecking(false); };
  }, [open, slug, start, end]);

  const spanSeconds = start && end
    ? (new Date(end).getTime() - new Date(start).getTime()) / 1000 : 0;
  // Shown while typing, before the server has an opinion. The SAVED value
  // always comes from the server's interval_seconds — see types.ts.
  const previewInterval = spanSeconds > 0 && target > 0 ? spanSeconds / target : null;
  const windowValid = spanSeconds > 0;

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const body = {
        name: name.trim(),
        description: description.trim() || null,
        camera_slug: slug,
        window_start: toIso(start),
        window_end: toIso(end),
        target_frames: target,
        jpeg_quality: quality,
        model_id: modelId || null,
        target_classes: modelId ? classes : [],
        min_confidence: modelId ? minConf : null,
        scan_interval_seconds: modelId ? scanEvery : null,
        ptz_preset_ids: ptzTime?.ptz ? presetIds : [],
      };
      const saved = existing
        ? await apiFetch<Dataset>(`/datasets/${existing.id}`, {
            method: 'PATCH', body: JSON.stringify(body) })
        : await apiFetch<Dataset>('/datasets', {
            method: 'POST', body: JSON.stringify(body) });
      onSaved(saved);
      onClose();
    } catch (e: any) {
      setError(e?.message || 'Could not save the dataset');
    } finally {
      setSaving(false);
    }
  }

  const filterValid = !modelId || classes.length > 0;
  const scans = modelId && spanSeconds > 0 ? Math.floor(spanSeconds / scanEvery) : 0;
  const canSave = !!name.trim() && !!slug && windowValid && target > 0
    && filterValid && scans <= 20000 && !saving;

  return (
    <Modal
      open={open}
      title={existing ? `Edit ${existing.name}` : 'New dataset'}
      onClose={onClose}
      width={620}
      footer={
        <div className="form-actions">
          <button className="btn btn-ghost" onClick={onClose} disabled={saving}>Cancel</button>
          <button className="btn btn-primary" onClick={save} disabled={!canSave}>
            {saving ? 'Saving…' : existing ? 'Save changes' : 'Create dataset'}
          </button>
        </div>
      }
    >
      {error && <div className="banner banner-crit" style={{ marginBottom: 12 }}>{error}</div>}

      <div className="form-group">
        <label>Name</label>
        <input value={name} onChange={e => setName(e.target.value)}
               placeholder="Gate 3 — morning traffic" maxLength={200} />
      </div>

      <div className="form-group">
        <label>Description <span className="hint">optional</span></label>
        <input value={description} onChange={e => setDescription(e.target.value)}
               placeholder="What is this training set for?" />
      </div>

      <div className="form-group">
        <label>Camera</label>
        <select value={slug} onChange={e => setSlug(e.target.value)}>
          <option value="">Select a camera…</option>
          {cameras.map((c: Camera) => (
            <option key={c.slug} value={c.slug}>{c.name} ({c.slug})</option>
          ))}
        </select>
      </div>

      <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
        <div className="form-group">
          <label>Window start</label>
          <input type="datetime-local" value={start} onChange={e => setStart(e.target.value)} />
        </div>
        <div className="form-group">
          <label>Window end</label>
          <input type="datetime-local" value={end} onChange={e => setEnd(e.target.value)} />
        </div>
      </div>
      {!windowValid && start && end && (
        <div className="hint" style={{ color: 'var(--danger, #c0392b)' }}>
          The window must end after it starts.
        </div>
      )}

      {/* Coverage, before the run rather than after it. */}
      {slug && windowValid && (
        <div style={{ margin: '4px 0 12px' }}>
          {checking ? (
            <div className="hint">Checking recording coverage…</div>
          ) : coverage ? (
            <div className={`banner ${coverage.covered ? '' : 'banner-warn'}`}
                 style={coverage.covered
                   ? { background: 'transparent', padding: 0, color: 'var(--muted)', fontSize: 12 }
                   : undefined}>
              {coverage.message}
              {coverage.known && coverage.earliest && coverage.latest && (
                <div className="hint" style={{ marginTop: 4 }}>
                  Recorded range: {new Date(coverage.earliest).toLocaleString()} →{' '}
                  {new Date(coverage.latest).toLocaleString()}
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}

      {/* PTZ camera: only frames while it was AT chosen presets — not the moves between them, not other views. */}
      {ptzTime?.ptz && windowValid && (
        <fieldset style={{ border: '1px solid var(--border)', borderRadius: 8, padding: '8px 12px', margin: '4px 0 12px' }}>
          <legend className="hint" style={{ padding: '0 6px' }}>PTZ presets <span className="hint">optional</span></legend>
          <div className="hint" style={{ marginBottom: 8 }}>
            Tick presets to take frames only while the camera was confirmed at them — each frame then says which
            preset it shows. None ticked: the whole window, moves included.
          </div>
          {ptzTime.presets.length === 0 ? (
            <div className="hint">This camera has no presets.</div>
          ) : (
            <div style={{ display: 'grid', gap: 4 }}>
              {ptzTime.presets.map(p => (
                <label key={p.preset_id} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
                  <input type="checkbox" checked={presetIds.includes(p.preset_id)}
                         onChange={e => setPresetIds(ids => e.target.checked ? [...ids, p.preset_id] : ids.filter(x => x !== p.preset_id))} />
                  <span style={{ flex: 1 }}>{p.name}</span>
                  <span className="hint" title="Time at this preset inside the window">
                    {p.seconds > 0 ? `${fmtSpan(p.seconds)} · ${p.visits} visit${p.visits === 1 ? '' : 's'}` : 'not visited in this window'}
                  </span>
                </label>
              ))}
            </div>
          )}
          {presetIds.length > 0 && (() => {
            const secs = ptzTime.presets.filter(p => presetIds.includes(p.preset_id)).reduce((a, p) => a + p.seconds, 0);
            return (
              <div className={secs > 0 ? 'hint' : 'banner banner-warn'} style={{ marginTop: 8 }}>
                {secs > 0
                  ? <>{fmtSpan(secs)} at the chosen preset{presetIds.length > 1 ? 's' : ''} in this window
                      {!modelId && target > 0 ? ` — about one frame every ${fmtSpan(secs / target)}` : ''}.</>
                  : 'The camera was never at the chosen preset(s) in this window — the run would give no frames.'}
                {ptzTime.source === 'position' && (
                  <div className="hint" style={{ marginTop: 4 }}>
                    This window is older than the camera's confirmed-view history, so "at the preset" comes from the
                    camera's own position report (not checked against the picture).
                  </div>
                )}
              </div>
            );
          })()}
        </fieldset>
      )}

      {/* Detection filter. Optional: without a model, frames are sampled evenly. */}
      <fieldset style={{ border: '1px solid var(--border)', borderRadius: 8,
                         padding: '8px 12px', margin: '4px 0 12px' }}>
        <legend className="hint" style={{ padding: '0 6px' }}>
          Detection filter <span className="hint">optional</span>
        </legend>
        <div className="hint" style={{ marginBottom: 8 }}>
          Upload a YOLO model and pick the classes to look for. The window is scanned
          and only frames where a chosen class is detected are saved.
        </div>
        {modelError && <div className="banner banner-warn" style={{ marginBottom: 8 }}>{modelError}</div>}
        <div className="form-group">
          <label>Model</label>
          <div style={{ display: 'flex', gap: 8 }}>
            <select value={modelId} style={{ flex: 1 }}
                    onChange={e => { setModelId(e.target.value); setClasses([]); }}>
              <option value="">None — sample evenly</option>
              {models.map(m => (
                <option key={m.id} value={m.id}>{m.name} ({m.format}, {m.classes.length} classes)</option>
              ))}
            </select>
            <label className="btn btn-ghost" style={{ cursor: 'pointer', margin: 0 }}>
              {uploading ? 'Uploading…' : 'Upload model'}
              <input type="file" accept=".pt,.onnx" hidden disabled={uploading}
                     onChange={e => {
                       const f = e.target.files?.[0];
                       e.target.value = '';
                       if (f) void uploadModel(f);
                     }} />
            </label>
          </div>
        </div>
        {model && (
          <>
            <div className="form-group">
              <label>Classes to collect</label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {model.classes.map(c => (
                  <label key={c} className="chip"
                         style={{ display: 'inline-flex', gap: 4, alignItems: 'center',
                                  padding: '2px 8px', border: '1px solid var(--border)',
                                  borderRadius: 12, cursor: 'pointer',
                                  background: classes.includes(c) ? 'var(--accent-soft, #dbeafe)' : undefined }}>
                    <input type="checkbox" checked={classes.includes(c)}
                           onChange={() => toggleClass(c)} />
                    {c}
                  </label>
                ))}
              </div>
              {classes.length === 0 && (
                <div className="hint" style={{ color: 'var(--danger, #c0392b)' }}>
                  Choose at least one class.
                </div>
              )}
            </div>
            <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
              <div className="form-group">
                <label>Minimum confidence</label>
                <input type="number" min={0.05} max={0.95} step={0.05} value={minConf}
                       onChange={e => setMinConf(Math.max(0.05, Math.min(0.95, +e.target.value || 0.35)))} />
              </div>
              <div className="form-group">
                <label>Scan every (seconds)</label>
                <input type="number" min={0.5} max={3600} step={0.5} value={scanEvery}
                       onChange={e => setScanEvery(Math.max(0.5, +e.target.value || 2))} />
              </div>
            </div>
            <div className="hint">
              {scans.toLocaleString()} frames will be scanned; at most {target.toLocaleString()} are kept.
              {scans > 20000 && (
                <span style={{ color: 'var(--danger, #c0392b)' }}>
                  {' '}Too many scans — raise the interval or shorten the window.
                </span>
              )}
            </div>
          </>
        )}
      </fieldset>

      <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
        <div className="form-group">
          <label>{modelId ? 'Max frames to keep' : 'Frames to collect'}</label>
          <input type="number" min={1} max={20000} value={target}
                 onChange={e => setTarget(Math.max(1, Math.min(20000, +e.target.value || 1)))} />
        </div>
        <div className="form-group">
          <label>JPEG quality</label>
          <input type="number" min={1} max={95} value={quality}
                 onChange={e => setQuality(Math.max(1, Math.min(95, +e.target.value || 85)))} />
        </div>
      </div>

      {/* The interval is the number that says how far apart the training images
          are; the estimate is what stops someone typing 20000 by accident. */}
      <div className="kv" style={{ marginTop: 4 }}>
        <div className="kv-row">
          <span>Sampling interval</span>
          <strong>{modelId ? fmtInterval(scanEvery) : fmtInterval(previewInterval)}</strong>
        </div>
        <div className="kv-row">
          <span>Estimated collection time</span>
          <strong>{fmtEstimate(modelId ? scans : target)}</strong>
        </div>
      </div>
      {target >= 2000 && (
        <div className="banner banner-warn" style={{ marginTop: 10 }}>
          {target.toLocaleString()} frames is {fmtEstimate(target)} of continuous
          extraction on the recorder, which is also recording your cameras
          while it runs. Collection continues in the background — you can leave
          this page.
        </div>
      )}
    </Modal>
  );
}
