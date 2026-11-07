/**
 * ImportDialog — send a collected dataset to Label Studio and/or S3.
 *
 * THE TWO DESTINATIONS ARE INDEPENDENT CHECKBOXES, not a mode switch. They
 * answer different questions — Label Studio so the frames can be annotated, S3
 * so they are kept somewhere durable — and the server runs them concurrently
 * with failures isolated. That is what makes "re-run just the half that failed"
 * possible, so the UI has to offer the halves separately and report them
 * separately.
 *
 * Credentials never appear here. The bucket and prefix are chosen per import
 * (so one appliance can feed different destinations); the AWS keys stay in the
 * server environment and are never sent to the browser.
 */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import type { Dataset, ExportConfig, ExportOutcome } from './types';

/** Did Label Studio report fewer tasks than the files it was sent? */
function fewerTasksThanSent(r: ExportOutcome): boolean {
  return r.tasks != null && r.sent != null && r.tasks < r.sent;
}

export function ImportDialog({ open, dataset, onClose, onDone }: {
  open: boolean;
  dataset: Dataset | null;
  onClose: () => void;
  onDone: (d: Dataset) => void;
}) {
  const [cfg, setCfg] = useState<ExportConfig | null>(null);
  const [toLs, setToLs] = useState(true);
  const [toS3, setToS3] = useState(true);
  const [projectId, setProjectId] = useState('');
  const [bucket, setBucket] = useState('');
  const [prefix, setPrefix] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Record<string, ExportOutcome> | null>(null);

  useEffect(() => {
    if (!open || !dataset) return;
    setError(null);
    setResult(null);
    setBusy(false);
    apiFetch<ExportConfig>('/datasets/labelstudio/config')
      .then(c => {
        setCfg(c);
        // Most specific first, mirroring the server's own resolution order:
        // what this dataset last used, then the appliance default.
        setProjectId(String(dataset.labelstudio_project_id ?? c.default_project_id ?? ''));
        setBucket(dataset.s3_bucket ?? c.default_bucket ?? '');
        setPrefix(dataset.s3_prefix ?? c.default_prefix ?? '');
        setToLs(c.configured);
        setToS3(true);
      })
      .catch(e => setError(e?.message || 'Could not read the export configuration'));
  }, [open, dataset]);

  async function run() {
    if (!dataset) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await apiFetch<{ dataset: Dataset; destinations: Record<string, ExportOutcome> }>(
        `/datasets/${dataset.id}/export`,
        {
          method: 'POST',
          body: JSON.stringify({
            to_labelstudio: toLs,
            to_s3: toS3,
            project_id: toLs && projectId ? Number(projectId) : null,
            s3_bucket: toS3 ? bucket.trim() : null,
            s3_prefix: toS3 ? prefix.trim() : null,
          }),
        },
      );
      setResult(res.destinations);
      onDone(res.dataset);
    } catch (e: any) {
      // A total failure comes back as a 502 whose detail carries both
      // destinations, so the per-destination reasons survive even then.
      const detail = e?.detail;
      if (detail && typeof detail === 'object' && detail.destinations) {
        setResult(detail.destinations);
      }
      setError(e?.message || 'The export failed');
    } finally {
      setBusy(false);
    }
  }

  if (!dataset) return null;
  const frames = dataset.frames_collected;
  const nothingChosen = !toLs && !toS3;

  return (
    <Modal
      open={open}
      title={`Import ${dataset.name}`}
      onClose={onClose}
      width={600}
      footer={
        <div className="form-actions">
          <button className="btn btn-ghost" onClick={onClose} disabled={busy}>
            {result ? 'Close' : 'Cancel'}
          </button>
          <button className="btn btn-primary" onClick={run}
                  disabled={busy || nothingChosen || (toLs && !cfg?.configured)}>
            {busy ? 'Importing…' : result ? 'Run again' : `Import ${frames} frames`}
          </button>
        </div>
      }
    >
      {error && <div className="banner banner-crit" style={{ marginBottom: 12 }}>{error}</div>}

      {/* ── Label Studio ── */}
      <div className="panel" style={{ marginBottom: 12 }}>
        <div className="panel-head">
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer' }}>
            <input type="checkbox" checked={toLs} disabled={!cfg?.configured}
                   onChange={e => setToLs(e.target.checked)} />
            <span className="panel-title">Label Studio — for annotation</span>
          </label>
        </div>
        <div className="panel-body">
          {cfg && !cfg.configured ? (
            <div className="banner banner-warn">
              Label Studio is not configured on this appliance. Set
              LABELSTUDIO_URL and LABELSTUDIO_API_KEY on the api service to
              enable it. You can still mirror to S3.
            </div>
          ) : (
            <>
              <div className="form-group">
                <label>Project id</label>
                <input value={projectId} onChange={e => setProjectId(e.target.value)}
                       placeholder="e.g. 3" inputMode="numeric" disabled={!toLs} />
                <div className="hint">
                  The number in the project's URL. The project must already exist
                  with the right label config — importing never creates one.
                </div>
              </div>
              {cfg?.url && <div className="hint">Server: {cfg.url}</div>}
            </>
          )}
        </div>
      </div>

      {/* ── S3 ── */}
      <div className="panel">
        <div className="panel-head">
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', cursor: 'pointer' }}>
            <input type="checkbox" checked={toS3} onChange={e => setToS3(e.target.checked)} />
            <span className="panel-title">S3 — for keeping</span>
          </label>
        </div>
        <div className="panel-body">
          <div className="form-grid" style={{ gridTemplateColumns: '1fr 1fr' }}>
            <div className="form-group">
              <label>Bucket</label>
              <input value={bucket} onChange={e => setBucket(e.target.value)}
                     placeholder="my-training-data" disabled={!toS3} maxLength={63} />
            </div>
            <div className="form-group">
              <label>Prefix <span className="hint">optional</span></label>
              <input value={prefix} onChange={e => setPrefix(e.target.value)}
                     placeholder="datasets/gate-3/" disabled={!toS3} maxLength={512} />
            </div>
          </div>
          <div className="hint">
            {cfg?.s3_credentials
              ? `Using the credentials configured on this appliance${cfg.s3_region ? ` (${cfg.s3_region})` : ''}.`
              : 'No AWS keys are set on this appliance — the upload will use an instance role or a mounted ~/.aws.'}
          </div>
        </div>
      </div>

      {nothingChosen && (
        <div className="hint" style={{ marginTop: 10, color: 'var(--danger, #c0392b)' }}>
          Choose at least one destination.
        </div>
      )}

      {/* Per-destination outcomes. One half failing must READ as one half
          failing — never as a blanket error that hides a successful upload. */}
      {result && (
        <div className="kv" style={{ marginTop: 14 }}>
          {Object.entries(result).map(([dest, r]) => (
            <div key={dest} className={`kv-row ${r.ok ? 'row-ok' : 'row-err'}`}>
              <span>{dest === 'labelstudio' ? 'Label Studio' : 'S3'}</span>
              <strong>
                {r.ok
                  ? dest === 'labelstudio'
                    ? `${r.tasks} tasks in project ${r.project_id}`
                    : `${r.uploaded} uploaded${r.skipped ? `, ${r.skipped} skipped` : ''}`
                  : `Failed — ${r.error}`}
              </strong>
            </div>
          ))}
        </div>
      )}
      {/* A partial import must not read as a clean one. Frames missing from the
          dataset's directory are skipped by both destinations; before this the
          dialog said "77 tasks" for a 1000-frame dataset and nothing else. */}
      {result && Object.entries(result).filter(([, r]) => r.ok && (r.missing_on_disk ?? 0) > 0)
        .map(([dest, r]) => (
          <div key={`missing-${dest}`} className="banner banner-warn" style={{ marginTop: 8 }}>
            {dest === 'labelstudio' ? 'Label Studio' : 'S3'}: {r.missing_on_disk} of {frames} frames
            were not sent — they are listed in this dataset but no longer on disk
            {r.missing_examples?.length ? ` (e.g. ${r.missing_examples[0]})` : ''}.
            Re-collect the dataset to restore them.
          </div>
        ))}
      {/* Label Studio took every batch (201) but made fewer tasks than it was
          sent files — a successful-looking import that is not one. */}
      {result?.labelstudio?.ok && fewerTasksThanSent(result.labelstudio) && (
        <div className="banner banner-warn" style={{ marginTop: 8 }}>
          Label Studio created {result.labelstudio.tasks} tasks from {result.labelstudio.sent} frames
          sent. Check project {result.labelstudio.project_id} before importing again.
        </div>
      )}
      {result && Object.values(result).some(r => !r.ok) && Object.values(result).some(r => r.ok) && (
        <div className="hint" style={{ marginTop: 8 }}>
          The successful half has been recorded. Untick the one that worked and
          run again to retry only what failed.
        </div>
      )}
    </Modal>
  );
}
