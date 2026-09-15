/**
 * MapModals.tsx — the sitemap upload and rename dialogs.
 *
 * The upload modal carries the client half of the dimension guard: it measures
 * the picked file in the browser so a bad plan is refused in the file picker
 * with a specific reason, rather than after a 10 MB round trip. The server
 * (backend/models.py — validate_sitemap_dimensions) remains the authority.
 */
import { useEffect, useState } from 'react';

import { Modal } from '@/components/Modal';

import { MAX_UPLOAD_BYTES, MIN_EDGE_PX, checkPlanDimensions } from './mapConstants';

export function UploadModal({ open, busy, onClose, onSubmit }: {
  open: boolean; busy: boolean; onClose: () => void; onSubmit: (name: string, file: File) => void;
}) {
  const [name, setName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [problem, setProblem] = useState('');
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);

  useEffect(() => { if (open) { setName(''); setFile(null); setProblem(''); setDims(null); } }, [open]);

  // Measure the picked file before it goes anywhere. SVG is exempt — it has no
  // pixel size to bound; the browser resolves it from width/height or viewBox.
  const pick = (f: File | null) => {
    setFile(f); setProblem(''); setDims(null);
    if (!f) return;
    if (f.size > MAX_UPLOAD_BYTES) {
      setProblem(`File is ${(f.size / 1024 / 1024).toFixed(1)} MB — the limit is ${MAX_UPLOAD_BYTES / 1024 / 1024} MB.`);
      return;
    }
    if (f.type === 'image/svg+xml') return;
    const url = URL.createObjectURL(f);
    const img = new Image();
    img.onload = () => {
      const w = img.naturalWidth, h = img.naturalHeight;
      URL.revokeObjectURL(url);
      setDims({ w, h });
      setProblem(checkPlanDimensions(w, h) || '');
    };
    img.onerror = () => { URL.revokeObjectURL(url); setProblem('That file is not a readable image.'); };
    img.src = url;
  };

  return (
    <Modal open={open} title="Upload site plan" onClose={onClose} width={420}
      footer={
        <>
          <button className="btn-subtle btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn-primary btn-sm" disabled={busy || !name.trim() || !file || !!problem}
            onClick={() => file && onSubmit(name.trim(), file)}>
            {busy ? <span className="spinner" /> : 'Upload'}
          </button>
        </>
      }>
      <label style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 5 }}>Name</label>
      <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Ground floor"
        style={{ width: '100%', marginBottom: 14 }} />
      <label style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 5 }}>Image</label>
      <input type="file" accept="image/png,image/jpeg,image/webp,image/svg+xml"
        onChange={e => pick(e.target.files?.[0] || null)} />
      {problem ? (
        <div style={{ marginTop: 10, fontSize: 12, color: 'var(--red)' }}>{problem}</div>
      ) : dims ? (
        <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--dim)' }}>
          {dims.w}×{dims.h}px · {(dims.w / dims.h).toFixed(2)}:1
        </div>
      ) : (
        <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--dim)' }}>
          PNG, JPEG, WebP or SVG · at least {MIN_EDGE_PX}px on the longest side · up to {MAX_UPLOAD_BYTES / 1024 / 1024} MB
        </div>
      )}
    </Modal>
  );
}

export function RenameModal({ open, busy, initialName, onClose, onSubmit }: {
  open: boolean; busy: boolean; initialName: string; onClose: () => void; onSubmit: (name: string) => void;
}) {
  const [name, setName] = useState(initialName);
  useEffect(() => { if (open) setName(initialName); }, [open, initialName]);

  return (
    <Modal open={open} title="Rename site plan" onClose={onClose} width={380}
      footer={
        <>
          <button className="btn-subtle btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn-primary btn-sm" disabled={busy || !name.trim() || name.trim() === initialName}
            onClick={() => onSubmit(name.trim())}>
            {busy ? <span className="spinner" /> : 'Save'}
          </button>
        </>
      }>
      <label style={{ display: 'block', fontSize: 12, color: 'var(--muted)', marginBottom: 5 }}>Name</label>
      <input value={name} onChange={e => setName(e.target.value)} style={{ width: '100%' }} />
    </Modal>
  );
}
