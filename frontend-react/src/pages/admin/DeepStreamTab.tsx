/**
 * DeepStreamTab — the detector models the DeepStream analytics pipeline holds.
 *
 * Two jobs: upload a new model (ONNX + labels file) to the pipeline, and see
 * which models it has, with a restart to load a change. Everything goes through
 * the VMS backend (routers/deepstream_admin.py), which adds the pipeline's
 * internal key — the browser never talks to :8081.
 *
 * Where the pipeline is comes first: an address saved here wins over the
 * server's DEEPSTREAM_API_URL (and the camera sync follows it too), so a
 * pipeline on another host or port is a setting, not an .env edit.
 *
 * A RESTART INTERRUPTS EVERY CAMERA ON THE PIPELINE, including ones the VMS
 * does not manage. So "restart after upload" is off by default, and the
 * explicit restart asks twice — on the screen, since the viewer has no confirm().
 */
import { useEffect, useRef, useState } from 'react';
import { useToast } from '@/components/Toast';
import { Modal } from '@/components/Modal';
import { addDeepstreamModel, getDeepstreamSettings, listDeepstreamModels, modelRows, restartDeepstream,
  saveDeepstreamSettings, testDeepstreamSettings, type DeepstreamSettings,
  type PipelineAnswer } from '@/lib/deepstream';

const cell = (v: unknown) => v === null || v === undefined ? '—'
  : typeof v === 'object' ? JSON.stringify(v) : String(v);

export function DeepStreamTab() {
  return <>
    <ConnectionPanel />
    <ModelsPanel />
  </>;
}

function ConnectionPanel() {
  const toast = useToast();
  const [cfg, setCfg] = useState<DeepstreamSettings | null>(null);
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [err, setErr] = useState('');

  useEffect(() => {
    let live = true;
    getDeepstreamSettings()
      .then(s => { if (live) { setCfg(s); setUrl(s.api_url ?? ''); } })
      .catch((e: any) => { if (live) setErr(e?.message || 'Could not load DeepStream settings'); });
    return () => { live = false; };
  }, []);

  const typed = url.trim() || null;
  const dirty = cfg != null && typed !== (cfg.api_url ?? null);

  async function test() {
    setBusy(true); setMessage('');
    try {
      const r = await testDeepstreamSettings(typed);
      setMessage(`Reached ${r.url}.${dirty ? ' Save to use this address.' : ''}`);
    } catch (e: any) {
      setMessage(e?.message || 'No answer from that address');
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true); setMessage('');
    try {
      const s = await saveDeepstreamSettings(typed);
      setCfg(s); setUrl(s.api_url ?? '');
      toast(`DeepStream address set to ${s.effective_url}`);
      window.dispatchEvent(new Event('deepstream-models-changed'));
    } catch (e: any) {
      toast(e?.message || 'Could not save the address', 'err');
    } finally {
      setBusy(false);
    }
  }

  if (err) return <div className="panel" style={{ marginBottom: 16 }}><div className="panel-body"><div className="empty">{err}</div></div></div>;

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-head"><div className="card-title">Pipeline connection</div></div>
      <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
        <label>DeepStream control API address
          <input value={url} disabled={busy || !cfg} placeholder={cfg?.default_url ?? 'http://127.0.0.1:8081'}
            onChange={e => setUrl(e.target.value)} style={{ display: 'block', width: '100%', maxWidth: 420 }} />
        </label>
        <div className="hint">
          {cfg && (cfg.api_url
            ? <>Using <strong>{cfg.effective_url}</strong>{cfg.updated_by && <> — set by {cfg.updated_by}</>}. Clear the field and save to go back to the server default ({cfg.default_url}).</>
            : <>Using the server default <strong>{cfg.default_url}</strong> (DEEPSTREAM_API_URL). Enter an address to override it.</>)}
          {' '}Camera sync to the pipeline uses this address too.
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn-ghost btn-sm" disabled={busy || !cfg} onClick={test}>Test connection</button>
          <button className="btn-primary btn-sm" disabled={busy || !dirty} onClick={save}>{busy ? 'Working…' : 'Save'}</button>
        </div>
        {message && <div role="status">{message}</div>}
      </div>
    </div>
  );
}

function UploadModelModal({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const [name, setName] = useState('');
  const [onnx, setOnnx] = useState<File | null>(null);
  const [labels, setLabels] = useState<File | null>(null);
  const [batchSize, setBatchSize] = useState(1);
  // Blank = let the pipeline pick its default (its curl omits it too).
  const [networkSize, setNetworkSize] = useState<number | null>(640);
  const [restart, setRestart] = useState(false);
  const [busy, setBusy] = useState(false);
  // Bumped after a successful upload to clear the <input type=file>s.
  const [formKey, setFormKey] = useState(0);

  const missing = [!name.trim() && 'name', !onnx && 'ONNX file', !labels && 'labels file'].filter(Boolean);

  async function upload() {
    if (!onnx || !labels) return;
    setBusy(true);
    try {
      await addDeepstreamModel({ name: name.trim(), onnx, labels, batchSize, networkSize, restart });
      toast(restart ? 'Model uploaded — DeepStream is restarting' : 'Model uploaded to DeepStream');
      setName(''); setOnnx(null); setLabels(null); setFormKey(k => k + 1);
      window.dispatchEvent(new CustomEvent('deepstream-models-changed', { detail: { restarting: restart } }));
      onClose();
    } catch (e: any) {
      toast(e?.message || 'Upload failed', 'err');
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open title="Upload model" width={600} onClose={() => { if (!busy) onClose(); }}>
      <div style={{ display: 'grid', gap: 16 }} key={formKey}>
        <div className="hint">An ONNX detector export and its labels file (one class name per line, in the model's class order).</div>
        <label>Model name
          <input value={name} disabled={busy} placeholder="e.g. school_v2"
            onChange={e => setName(e.target.value)} style={{ display: 'block' }} />
        </label>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }}>
          <label>ONNX model (.onnx)
            <input type="file" accept=".onnx" disabled={busy} style={{ display: 'block' }}
              onChange={e => setOnnx(e.target.files?.[0] ?? null)} />
          </label>
          <label>Labels (.txt)
            <input type="file" accept=".txt,text/plain" disabled={busy} style={{ display: 'block' }}
              onChange={e => setLabels(e.target.files?.[0] ?? null)} />
          </label>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16 }}>
          <label>Batch size
            <input type="number" min={1} max={64} value={batchSize} disabled={busy}
              onChange={e => setBatchSize(Number(e.target.value))} style={{ width: 110, display: 'block' }} />
          </label>
          <label>Network size (px)
            <input type="number" min={32} max={4096} step={32} value={networkSize ?? ''} disabled={busy}
              placeholder="default"
              onChange={e => setNetworkSize(e.target.value ? Number(e.target.value) : null)}
              style={{ width: 110, display: 'block' }} />
          </label>
        </div>
        <label><input type="checkbox" checked={restart} disabled={busy}
          onChange={e => setRestart(e.target.checked)} /> Restart DeepStream after upload to load this model</label>
        {restart && <div className="hint">A restart briefly stops analytics on every camera the pipeline runs.</div>}
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn-primary btn-sm" disabled={busy || missing.length > 0} onClick={upload}>
            {busy ? 'Uploading…' : 'Upload model'}
          </button>
          <button className="btn-ghost btn-sm" disabled={busy} onClick={onClose}>Cancel</button>
          {missing.length > 0 && !busy && <span className="hint">Needs {missing.join(', ')}.</span>}
        </div>
      </div>
    </Modal>
  );
}

function ModelsPanel() {
  const [uploadOpen, setUploadOpen] = useState(false);
  const toast = useToast();
  const [answer, setAnswer] = useState<PipelineAnswer | null>(null);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [restarting, setRestarting] = useState(false);

  async function reload() {
    setLoading(true);
    try {
      setAnswer(await listDeepstreamModels());
      setErr('');
    } catch (e: any) {
      setErr(e?.message || 'Could not load DeepStream models');
    } finally {
      setLoading(false);
    }
  }

  // After a restart the pipeline is gone for a while; re-list a few times so
  // the table catches it coming back with the new model, instead of showing
  // the "unreachable" error from the first attempt.
  const timers = useRef<number[]>([]);
  function reloadAfterRestart() {
    timers.current.forEach(clearTimeout);
    timers.current = [5_000, 15_000, 30_000, 60_000].map(ms => window.setTimeout(() => void reload(), ms));
  }

  useEffect(() => {
    void reload();
    const onChange = (e: Event) => {
      void reload();
      if ((e as CustomEvent).detail?.restarting) reloadAfterRestart();
    };
    window.addEventListener('deepstream-models-changed', onChange);
    return () => {
      window.removeEventListener('deepstream-models-changed', onChange);
      timers.current.forEach(clearTimeout);
    };
  }, []);

  async function restart() {
    setRestarting(true);
    try {
      await restartDeepstream();
      toast('DeepStream restart requested');
      setConfirming(false);
      reloadAfterRestart();
    } catch (e: any) {
      toast(e?.message || 'Restart failed', 'err');
    } finally {
      setRestarting(false);
    }
  }

  const list = answer == null ? null : modelRows(answer);
  const rows = list?.rows ?? null;
  // `name` first, the rest in the order the pipeline sends them.
  const columns = rows
    ? [...new Set(['name', ...rows.flatMap(r => Object.keys(r))])].filter(c => rows.some(r => c in r))
    : [];

  return (
    <div className="panel">
      <div className="panel-head" style={{ flexWrap: 'wrap', gap: 12 }}>
        <div className="card-title">Models on DeepStream</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginLeft: 'auto' }}>
          <button className="btn-primary btn-sm" onClick={() => setUploadOpen(true)}>Upload model</button>
          <button className="btn-ghost btn-sm" disabled={loading} onClick={() => void reload()}>
            {loading ? 'Loading…' : 'Refresh'}
          </button>
          {!confirming && <button className="btn-ghost btn-sm" disabled={restarting}
            onClick={() => setConfirming(true)}>Restart DeepStream</button>}
        </div>
      </div>
      <div className="panel-body" style={{ display: 'grid', gap: 12 }}>
        {uploadOpen && <UploadModelModal onClose={() => setUploadOpen(false)} />}
        {confirming && (
          <div role="alert" style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center' }}>
            <span>Restarting stops analytics on every camera the pipeline runs until it comes back. Continue?</span>
            <button className="btn-primary btn-sm" disabled={restarting} onClick={restart}>
              {restarting ? 'Restarting…' : 'Yes, restart'}
            </button>
            <button className="btn-ghost btn-sm" disabled={restarting} onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        )}
        {list?.active && <div>Running: <strong>{list.active}</strong></div>}
        {err ? <div className="empty">{err}<div className="hint">Check the address under Pipeline connection.</div></div>
          : answer == null ? <div className="empty"><span className="spinner" /><span style={{ marginLeft: 8 }}>Loading…</span></div>
          : rows && rows.length === 0 ? <div className="empty">DeepStream reports no models.</div>
          : rows ? (
            <div className="table-wrap">
              <table>
                <thead><tr>{columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
                <tbody>{rows.map((r, i) => (
                  <tr key={i} style={r.active === true ? { fontWeight: 600 } : undefined}>
                    {columns.map(c => <td key={c}>{cell(r[c])}</td>)}
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(answer, null, 2)}</pre>}
        {rows && !Array.isArray(answer) && (
          <details>
            <summary className="hint">Full pipeline answer</summary>
            <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{JSON.stringify(answer, null, 2)}</pre>
          </details>
        )}
      </div>
    </div>
  );
}
