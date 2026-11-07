/**
 * DataCollectionPage — sample training images from recorded footage.
 *
 * A DATASET IS A RECIPE, NOT A FOLDER: "camera X, 10:00-12:00, 200 frames".
 * Collecting evaluates it. Raising the frame count and re-collecting re-samples
 * the same window more finely, as long as the footage is still inside
 * retention — which is why the list leads with the window and the sampling
 * interval rather than with a file count.
 *
 * Structure: list → DatasetDetail → ImportDialog.
 */
import { LoadingSkeleton } from '@/components/LoadingSkeleton';
import { ViewTransition } from '@/components/ViewTransition';
import { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { useToast } from '@/components/Toast';
import { fmtBytes } from '@/lib/format';
import { DatasetDetail } from './DatasetDetail';
import { DatasetForm } from './DatasetForm';
import { ImportDialog } from './ImportDialog';
import { AnnotationsTab } from './AnnotationsTab';
import { fmtInterval, statusBadge, type Dataset } from './types';

const LIST_POLL_MS = 4000;

export function DataCollectionPage() {
  const toast = useToast();
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [pane, setPane] = useState<'datasets' | 'annotations'>('datasets');
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<Dataset | null>(null);
  const [importing, setImporting] = useState<Dataset | null>(null);
  const [cancelling, setCancelling] = useState<number | null>(null);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const rows = await apiFetch<Dataset[]>('/datasets');
      setDatasets(rows);
      setError(null);
    } catch (e: any) {
      setError(e?.message || 'Could not load datasets');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Keep the LIST fresh only while something on it is actually running; the
  // detail view does its own faster polling when it is open.
  const anyCollecting = datasets.some(d => d.status === 'collecting');
  useEffect(() => {
    if (!anyCollecting || selectedId !== null) return;
    const t = setInterval(load, LIST_POLL_MS);
    return () => clearInterval(t);
  }, [anyCollecting, selectedId, load]);

  const upsert = useCallback((d: Dataset) => {
    setDatasets(list => {
      const i = list.findIndex(x => x.id === d.id);
      if (i === -1) return [d, ...list];
      const next = [...list];
      next[i] = d;
      return next;
    });
  }, []);

  const selected = selectedId !== null
    ? datasets.find(d => d.id === selectedId) ?? null : null;

  async function cancelRun(d: Dataset) {
    // Frames already written are kept, so this is not the destructive action
    // `remove` is — but it does end a run that may have taken hours, so it
    // still asks.
    if (!confirm(
      `Stop collecting "${d.name}"?\n\n` +
      `The ${d.frames_collected} frames collected so far are kept. ` +
      `You can start the collection again later.`)) return;
    setCancelling(d.id);
    try {
      const row = await apiFetch<Dataset>(`/datasets/${d.id}/cancel`, { method: 'POST' });
      upsert(row);
    } catch (e: any) {
      setError(e?.message || 'Could not cancel the collection');
    } finally {
      setCancelling(null);
    }
  }

  // A collecting dataset cannot be deleted (the API answers 409), so it is not
  // selectable either. Showing a checkbox that leads to a guaranteed refusal is
  // worse than showing none — Stop it first, then delete.
  const deletable = (d: Dataset) => d.status !== 'collecting';
  const selectable = datasets.filter(deletable);
  const allPicked = selectable.length > 0 && selectable.every(d => picked.has(d.id));
  const pickedRows = selectable.filter(d => picked.has(d.id));
  const pickedBytes = pickedRows.reduce((total, d) => total + (d.bytes_total ?? 0), 0);
  const unknownSizes = pickedRows.filter(d => d.bytes_total == null).length;
  const pickedSize = unknownSizes === 0
    ? fmtBytes(pickedBytes)
    : unknownSizes === pickedRows.length
      ? 'Size unknown'
      : `${fmtBytes(pickedBytes)} + unknown size`;

  function togglePick(id: number) {
    setPicked(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setPicked(allPicked ? new Set() : new Set(selectable.map(d => d.id)));
  }

  async function removePicked() {
    const rows = pickedRows;
    if (rows.length === 0) return;
    const frames = rows.reduce((n, d) => n + (d.frames_collected || 0), 0);
    if (!confirm(
      `Delete ${rows.length} dataset${rows.length > 1 ? 's' : ''}?\n\n` +
      rows.map(d => `  • ${d.name}`).join('\n') +
      `\n\nSelected data: ${pickedSize}.\n` +
      `This removes them and their ${frames} collected frames from this ` +
      `appliance. Anything already imported into Label Studio or mirrored to ` +
      `S3 is not touched.`
    )) return;

    setBulkBusy(true);
    const gone: number[] = [];
    const failed: string[] = [];
    // SEQUENTIAL, not Promise.all: each delete also removes a frames directory
    // off disk, and firing twenty of those at once on an appliance that is
    // usually also recording buys nothing but contention.
    for (const d of rows) {
      try {
        await apiFetch(`/datasets/${d.id}`, { method: 'DELETE' });
        gone.push(d.id);
      } catch (e: any) {
        failed.push(`${d.name}: ${e?.message || 'failed'}`);
      }
    }
    if (gone.length) {
      setDatasets(list => list.filter(x => !gone.includes(x.id)));
      if (selectedId !== null && gone.includes(selectedId)) setSelectedId(null);
    }
    // Keep whatever could not be deleted still ticked, so a retry does not mean
    // finding them again in the list.
    setPicked(new Set(rows.filter(d => !gone.includes(d.id)).map(d => d.id)));
    setBulkBusy(false);
    if (failed.length === 0) {
      toast(`Deleted ${gone.length} dataset${gone.length > 1 ? 's' : ''}`);
    } else {
      toast(`Deleted ${gone.length}, ${failed.length} failed — ${failed[0]}`, 'err');
    }
  }

  async function remove(d: Dataset) {
    if (!confirm(
      `Delete "${d.name}"?\n\nThis removes the dataset and its ${d.frames_collected} ` +
      `collected frames from this appliance. Anything already imported into ` +
      `Label Studio or mirrored to S3 is not touched.`
    )) return;
    try {
      await apiFetch(`/datasets/${d.id}`, { method: 'DELETE' });
      setDatasets(list => list.filter(x => x.id !== d.id));
      setPicked(prev => { const n = new Set(prev); n.delete(d.id); return n; });
      if (selectedId === d.id) setSelectedId(null);
      toast('Dataset deleted');
    } catch (e: any) {
      toast(e?.message || 'Could not delete the dataset', 'err');
    }
  }

  if (selected) {
    return (
      <>
        <DatasetDetail
          dataset={selected}
          onChanged={upsert}
          onBack={() => setSelectedId(null)}
          onEdit={() => { setEditing(selected); setFormOpen(true); }}
          onImport={() => setImporting(selected)}
          onDelete={() => remove(selected)}
        />
        <DatasetForm
          open={formOpen} existing={editing}
          onClose={() => { setFormOpen(false); setEditing(null); }}
          onSaved={upsert}
        />
        <ImportDialog
          open={!!importing} dataset={importing}
          onClose={() => setImporting(null)}
          onDone={upsert}
        />
      </>
    );
  }

  if (pane === 'annotations') {
    return (
      <ViewTransition view={pane}>
        <div className="page-head">
          <div className="page-head-l">
            <p className="subtitle" style={{ margin: 0 }}>
              Boxes drawn on Playback, with what each one is labelled as.
            </p>
          </div>
        </div>
        <div className="tabs">
          <button type="button" aria-pressed="false" className="tab" onClick={() => setPane('datasets')}>Datasets</button>
          <button type="button" aria-pressed="true" className="tab active">Annotations</button>
        </div>
        <AnnotationsTab />
      </ViewTransition>
    );
  }

  return (
    <ViewTransition view={pane}>
      <div className="page-head">
        <div className="page-head-l">
          <p className="subtitle" style={{ margin: 0 }}>
            Sample training images from recorded footage, then send them for
            annotation or keep them in S3.
          </p>
        </div>
        <div className="page-head-actions">
          <button className="btn btn-primary"
                  onClick={() => { setEditing(null); setFormOpen(true); }}>
            New dataset
          </button>
        </div>
      </div>

      {/* Two panes, one page: a dataset is frames sampled in bulk, an
          annotation is one box an operator drew mid-investigation. Different
          doors into the same training material, so they live together. */}
      <div className="tabs">
        <button type="button" aria-pressed="true" className="tab active">Datasets</button>
        <button type="button" aria-pressed="false" className="tab" onClick={() => setPane('annotations')}>Annotations</button>
      </div>

      {error && <div className="banner banner-crit">{error}</div>}

      {loading ? (
        <LoadingSkeleton label="Loading datasets…" count={5} />
      ) : datasets.length === 0 ? (
        <div className="emptystate">
          <h3>No datasets yet</h3>
          <p>
            A dataset is a recipe — a camera, a time window, and how many frames
            to pull from it. Create one, collect it, and the frames are ready to
            annotate.
          </p>
          <button className="btn btn-primary"
                  onClick={() => { setEditing(null); setFormOpen(true); }}>
            New dataset
          </button>
        </div>
      ) : (
        <div className="table-wrap">
          {pickedRows.length > 0 && (
            <div className="banner" style={{
              display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, marginBottom: 8,
            }}>
              <strong>{pickedRows.length} selected · {pickedSize}</strong>
              <button className="btn btn-danger btn-sm" disabled={bulkBusy}
                      onClick={() => void removePicked()}>
                {bulkBusy ? 'Deleting…' : `Delete ${pickedRows.length}`}
              </button>
              <button className="btn btn-ghost btn-sm" disabled={bulkBusy}
                      onClick={() => setPicked(new Set())}>
                Clear
              </button>
            </div>
          )}
          <table>
            <thead>
              <tr>
                <th style={{ width: 32 }}>
                  <input
                    type="checkbox"
                    checked={allPicked}
                    // Ticked-but-not-all reads as partial rather than "none",
                    // which is what an operator expects after picking a few.
                    ref={el => { if (el) el.indeterminate = picked.size > 0 && !allPicked; }}
                    disabled={selectable.length === 0}
                    onChange={toggleAll}
                    aria-label="Select all datasets"
                  />
                </th>
                <th>Name</th>
                <th>Camera</th>
                <th>Window</th>
                <th>Frames</th>
                {/* The interval sits beside the count on purpose — it is what
                    tells an operator how dense the training set actually is. */}
                <th>Interval</th>
                <th>Exported</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {datasets.map(d => {
                const badge = statusBadge(d.status);
                return (
                  <tr key={d.id} onClick={() => setSelectedId(d.id)} style={{ cursor: 'pointer' }}>
                    <td onClick={e => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        checked={picked.has(d.id)}
                        disabled={!deletable(d)}
                        title={deletable(d) ? undefined
                                            : 'Stop the collection before deleting'}
                        onChange={() => togglePick(d.id)}
                        aria-label={`Select ${d.name}`}
                      />
                    </td>
                    <td>
                      <strong>{d.name}</strong>
                      {d.description && <div className="hint">{d.description}</div>}
                    </td>
                    <td>{d.camera_slug}</td>
                    <td>
                      {new Date(d.window_start).toLocaleString()}
                      <div className="hint">→ {new Date(d.window_end).toLocaleString()}</div>
                    </td>
                    <td>
                      {d.status === 'collecting'
                        ? `${d.frames_collected} / ${d.target_frames}`
                        : d.frames_collected || d.target_frames}
                      {d.frames_missing > 0 && (
                        <div className="hint">{d.frames_missing} missing</div>
                      )}
                      {d.bytes_total != null && (
                        <div className="hint">{fmtBytes(d.bytes_total)}</div>
                      )}
                    </td>
                    <td>{fmtInterval(d.interval_seconds)}</td>
                    <td>
                      {d.labelstudio_exported_at && (
                        <div className="hint">LS · {d.labelstudio_task_count} tasks</div>
                      )}
                      {d.s3_uploaded_at && (
                        <div className="hint">S3 · {d.s3_uploaded_count} objects</div>
                      )}
                      {!d.labelstudio_exported_at && !d.s3_uploaded_at && '—'}
                    </td>
                    <td>
                      <span className={`badge ${badge.cls}`}>
                        <span className="badge-dot" />{badge.label}
                      </span>
                      {d.status === 'collecting' && (
                        // stopPropagation: the whole row opens the detail pane,
                        // and clicking Stop must not also navigate.
                        <button
                          className="btn btn-ghost btn-sm"
                          style={{ marginLeft: 8 }}
                          disabled={cancelling === d.id}
                          onClick={e => { e.stopPropagation(); void cancelRun(d); }}
                        >
                          {cancelling === d.id ? 'Stopping…' : 'Stop'}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <DatasetForm
        open={formOpen} existing={editing}
        onClose={() => { setFormOpen(false); setEditing(null); }}
        onSaved={d => { upsert(d); setSelectedId(d.id); }}
      />
    </ViewTransition>
  );
}
