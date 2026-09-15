/**
 * CsvImport.tsx — bulk camera import, ported from the legacy page-bulk
 * (processCsvFile / parseCsv / dryRun / submitBulk / renderPreview).
 *
 * One difference from the legacy port, on purpose: the legacy parsed the CSV in
 * the browser with `split('\n')` + `split(',')`, which mangles any quoted field
 * containing a comma. The API's /cameras/bulk/csv endpoint already parses an
 * uploaded file with Python's csv.DictReader, so we upload the file itself and
 * let the parser that actually works do the job.
 *
 * Validation is ALL-OR-NOTHING server-side: if any row is bad, nothing is
 * inserted. So "Validate" is not a nicety here — it is how you find the one bad
 * row that is blocking the other 49.
 */
import { useRef, useState } from 'react';
import { apiUpload } from '@/lib/api';
import { useToast } from '@/components/Toast';
import { LAWFUL_BASES } from './useDiscovery';

interface RowResult {
  row: number;
  name: string | null;
  success: boolean;
  slug: string | null;
  error: string | null;
}
interface BulkResp {
  total: number;
  success_count: number;
  error_count: number;
  dry_run: boolean;
  results: RowResult[];
}

export function CsvImport({ onImported }: { onImported: (slugs: string[]) => void }) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState<'dry' | 'real' | null>(null);
  const [result, setResult] = useState<BulkResp | null>(null);
  const [err, setErr] = useState('');
  // DPDP: a registered camera must carry a lawful basis + purpose. These are
  // applied to every imported row that doesn't supply its own — the batch-level
  // compliance record for the whole import.
  const [lawfulBasis, setLawfulBasis] = useState('');
  const [purpose, setPurpose] = useState('');

  function pick(f: File | null) {
    setFile(f);
    setResult(null);
    setErr('');
  }

  const complianceReady = lawfulBasis !== '' && purpose.trim() !== '';

  async function submit(dry: boolean) {
    if (!file) { toast('Choose a CSV file first', 'err'); return; }
    if (!complianceReady) { toast('Set a lawful basis and purpose for the batch first', 'err'); return; }
    setBusy(dry ? 'dry' : 'real');
    setErr('');
    try {
      const form = new FormData();
      form.append('file', file);
      const qs = new URLSearchParams({
        dry_run: String(dry),
        lawful_basis: lawfulBasis,
        purpose: purpose.trim(),
      });
      const r = await apiUpload<BulkResp>(`/cameras/bulk/csv?${qs}`, form);
      setResult(r);
      if (!dry && r.error_count === 0) {
        toast(`${r.success_count} camera${r.success_count === 1 ? '' : 's'} added`);
        onImported(r.results.filter(x => x.success && x.slug).map(x => x.slug as string));
      } else if (!dry) {
        toast('Nothing was imported — fix the errors below', 'err');
      }
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(null);
    }
  }

  const clean = result && result.error_count === 0;

  return (
    <div>
      <p style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 12 }}>
        Upload a CSV with one camera per row. Header: <code>name,rtsp_url,slug</code> —{' '}
        <code>slug</code> is optional and is generated from the name when omitted. Credentials may be embedded
        in the RTSP URL. A row may also carry its own <code>lawful_basis</code>/<code>purpose</code>; otherwise
        the batch values below apply.
      </p>

      <div style={{ display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
        <label style={{ flex: '1 1 200px' }}>
          <span style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>
            Lawful basis (DPDP) — required
          </span>
          <select value={lawfulBasis} onChange={e => setLawfulBasis(e.target.value)} style={{ width: '100%' }}>
            <option value="">Select a lawful basis…</option>
            {LAWFUL_BASES.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
        </label>
        <label style={{ flex: '2 1 260px' }}>
          <span style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>
            Purpose (DPDP) — required
          </span>
          <input value={purpose} onChange={e => setPurpose(e.target.value)}
            placeholder="Why these cameras record (e.g. premises security)" style={{ width: '100%' }} />
        </label>
      </div>

      <label
        onDragOver={e => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={e => {
          e.preventDefault();
          setDrag(false);
          pick(e.dataTransfer.files?.[0] ?? null);
        }}
        style={{
          display: 'block', padding: '26px 18px', textAlign: 'center', cursor: 'pointer',
          border: `1.5px dashed ${drag ? 'var(--accent)' : 'var(--border2)'}`,
          background: drag ? 'var(--accentSoft)' : 'transparent',
          borderRadius: 10, marginBottom: 12,
        }}
      >
        <input ref={fileRef} type="file" accept=".csv,text/csv" style={{ display: 'none' }}
               onChange={e => pick(e.target.files?.[0] ?? null)} />
        <div style={{ fontSize: 20, color: 'var(--dim)' }}>⤓</div>
        <div style={{ fontSize: 13, marginTop: 6 }}>
          {file ? <b>{file.name}</b> : 'Drop a CSV here, or click to choose'}
        </div>
        {file && (
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 3 }}>
            {(file.size / 1024).toFixed(1)} KB — click to replace
          </div>
        )}
      </label>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <button className="btn-ghost btn-sm" disabled={!file || !complianceReady || !!busy} onClick={() => submit(true)}>
          {busy === 'dry' ? <span className="spinner" /> : 'Validate (dry run)'}
        </button>
        <button className="btn-primary btn-sm" disabled={!file || !complianceReady || !!busy} onClick={() => submit(false)}>
          {busy === 'real'
            ? <span className="spinner" />
            : clean ? `Import ${result!.success_count} camera${result!.success_count === 1 ? '' : 's'}` : 'Import'}
        </button>
        {result && (
          <span style={{ fontSize: 12, marginLeft: 4 }}>
            <b style={{ color: 'var(--green)' }}>{result.success_count} ok</b>
            {result.error_count > 0 && (
              <> · <b style={{ color: 'var(--red)' }}>{result.error_count} error
                {result.error_count === 1 ? '' : 's'}</b></>
            )}
            {result.dry_run && (
              <span style={{ color: 'var(--muted)', fontStyle: 'italic' }}> (dry run)</span>
            )}
          </span>
        )}
      </div>

      {err && <div style={{ color: 'var(--red)', fontSize: 12.5, marginTop: 10 }}>{err}</div>}

      {result && result.error_count > 0 && (
        <div className="d-hint" style={{ marginTop: 10 }}>
          Import is all-or-nothing — <b>nothing was added</b>. Fix the rows below and upload again.
        </div>
      )}

      {result && (
        <div className="table-wrap" style={{ marginTop: 12, maxHeight: 320, overflowY: 'auto' }}>
          <table>
            <thead>
              <tr><th style={{ width: 46 }}>Row</th><th>Name</th><th>Slug</th><th>Result</th></tr>
            </thead>
            <tbody>
              {result.results.map(r => (
                <tr key={r.row}>
                  <td style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--dim)' }}>{r.row}</td>
                  <td style={{ fontSize: 12.5 }}>{r.name || '—'}</td>
                  <td style={{ fontFamily: 'var(--mono)', fontSize: 11, color: 'var(--muted)' }}>
                    {r.slug || '—'}
                  </td>
                  <td style={{ fontSize: 12 }}>
                    {r.success
                      ? <span style={{ color: 'var(--green)' }}>✓</span>
                      : <span style={{ color: 'var(--red)' }}>✗ {r.error}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
