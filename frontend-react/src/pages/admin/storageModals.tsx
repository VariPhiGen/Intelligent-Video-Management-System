/**
 * storageModals.tsx — the three destructive dialogs StorageTab owns: bulk
 * purge, fleet-wide retention/grooming, and the storage size cap.
 *
 * Extracted because each is a confirmation flow with its own typed-DELETE gate
 * and its own preview of what will be erased. Inline they were 130 lines of
 * JSX between the table and the end of the page, which made the destructive
 * paths hard to find and easy to skim past.
 *
 * They stay controlled by the page (props in, callbacks out) — the state is
 * bound up with the table's selection and the fleet's current settings.
 */
import { Modal } from '@/components/Modal';
import type { Camera } from '@/lib/types';

import { gb, humanSize, type NvrStorage } from './storageBits';

export function PurgeModal({
  confirming, setConfirming, selected, nameOf, selBytes, typed, setTyped, purge, busy,
}: {
  confirming: boolean;
  setConfirming: (v: boolean) => void;
  /** [slug, bytes] for each ticked row, in table order. */
  selected: [string, number][];
  nameOf: (slug: string) => string;
  selBytes: number;
  typed: string;
  setTyped: (v: string) => void;
  purge: () => void;
  busy: boolean;
}) {
  return (
      <Modal open={confirming} title="Delete footage permanently" onClose={() => setConfirming(false)} width={480}>
        <div style={{ fontSize: 13, marginBottom: 12 }}>
          Every recorded segment for {selected.length === 1 ? 'this camera' : `these ${selected.length} cameras`} will
          be erased from disk immediately. <b>This cannot be undone</b> — there is no trash and no backup.
        </div>
        <div style={{
          maxHeight: 150, overflowY: 'auto', border: '1px solid var(--border2)',
          borderRadius: 6, padding: '8px 10px', marginBottom: 12,
        }}>
          {selected.map(([slug, bytes]) => (
            <div key={slug} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12.5, padding: '2px 0' }}>
              <span>{nameOf(slug)}</span>
              <span style={{ fontFamily: 'var(--mono)', color: 'var(--muted)' }}>{gb(bytes)}</span>
            </div>
          ))}
        </div>
        <div style={{ fontSize: 12.5, marginBottom: 12 }}>
          Frees <b style={{ fontFamily: 'var(--mono)' }}>{gb(selBytes)}</b>. Cameras that are still enabled will begin
          accumulating new footage right away.
        </div>
        <div className="form-group full">
          <label>Type DELETE to confirm</label>
          <input value={typed} onChange={e => setTyped(e.target.value)} autoComplete="off" placeholder="DELETE" />
        </div>
        <div className="form-actions">
          <button className="btn-danger" onClick={purge} disabled={busy || typed !== 'DELETE'}>
            {busy ? 'Deleting…' : `Delete ${gb(selBytes)}`}
          </button>
          <button className="btn-ghost" onClick={() => setConfirming(false)} disabled={busy}>Cancel</button>
        </div>
      </Modal>
  );
}

export function BulkRetentionModal({
  bulkOpen, setBulkOpen, applying, gGroom, setGGroom, groomPlaceholder,
  gRetention, setGRetention, retentionPlaceholder, validRet, validGroom,
  loweredRetentionCams, gRetVal, groomsSoonerCams, bulkTyped, setBulkTyped,
  applyGlobal, canApply, cameras,
}: {
  bulkOpen: boolean;
  setBulkOpen: (v: boolean) => void;
  applying: boolean;
  gGroom: string;
  setGGroom: (v: string) => void;
  groomPlaceholder: string;
  gRetention: string;
  setGRetention: (v: string) => void;
  retentionPlaceholder: string;
  validRet: boolean;
  validGroom: boolean;
  /** Cameras whose footage this change would delete — gates the DELETE field. */
  loweredRetentionCams: Camera[];
  gRetVal: number | null;
  groomsSoonerCams: Camera[];
  bulkTyped: string;
  setBulkTyped: (v: string) => void;
  applyGlobal: () => void;
  canApply: boolean;
  cameras: Camera[];
}) {
  return (
    <Modal open={bulkOpen} title="Bulk retention & grooming" width={480}
        onClose={() => { if (!applying) setBulkOpen(false); }}>
        <div className="d-hint" style={{ marginTop: 0, marginBottom: 14 }}>
          Sets the retention and grooming windows for <b>every</b> camera at once, <b>overriding</b> each
          camera's individual setting. Leave a field blank to leave it unchanged. Per-camera overrides can
          still be adjusted under Config → Recording.
        </div>
        <div className="form-group full">
          <label>Keep full quality for (grooming)</label>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="number" min={1} max={3650} step={1} value={gGroom}
              onChange={e => setGGroom(e.target.value)} placeholder={groomPlaceholder} style={{ maxWidth: 200 }} />
            <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>days</span>
          </span>
        </div>
        <div className="form-group full">
          <label>Keep footage for (retention)</label>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="number" min={1} max={3650} step={1} value={gRetention}
              onChange={e => setGRetention(e.target.value)} placeholder={retentionPlaceholder} style={{ maxWidth: 200 }} />
            <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>days</span>
          </span>
        </div>
        {(!validRet || !validGroom) && (
          <div style={{ color: 'var(--red)', fontSize: 11.5, marginBottom: 8 }}>
            Enter a whole number of days between 1 and 3650, or leave the field blank.
          </div>
        )}
        {loweredRetentionCams.length > 0 && (
          <div style={{ fontSize: 13, margin: '4px 0 12px', lineHeight: 1.6 }}>
            <b style={{ color: 'var(--red)' }}>{loweredRetentionCams.length} camera{loweredRetentionCams.length !== 1 ? 's' : ''}</b>{' '}
            currently keep footage longer than {gRetVal} days — their older footage is <b>permanently deleted</b>{' '}
            within the hour. This cannot be undone; there is no trash and no backup.
          </div>
        )}
        {groomsSoonerCams.length > 0 && (
          <div style={{ fontSize: 13, margin: '4px 0 12px', lineHeight: 1.6 }}>
            {groomsSoonerCams.length} camera{groomsSoonerCams.length !== 1 ? 's' : ''} will start compressing footage
            to keyframe-only sooner — irreversible quality reduction, though grooming deletes no footage.
          </div>
        )}
        {loweredRetentionCams.length > 0 && (
          <div className="form-group full">
            <label>Type DELETE to confirm</label>
            <input value={bulkTyped} onChange={e => setBulkTyped(e.target.value)} autoComplete="off" placeholder="DELETE" />
          </div>
        )}
        <div className="form-actions">
          <button className={loweredRetentionCams.length > 0 ? 'btn-danger' : 'btn-primary'} onClick={applyGlobal}
            disabled={!canApply || applying || (loweredRetentionCams.length > 0 && bulkTyped !== 'DELETE')}>
            {applying ? 'Applying…' : `Apply to all ${cameras.length} camera${cameras.length !== 1 ? 's' : ''}`}
          </button>
          <button className="btn-ghost" onClick={() => setBulkOpen(false)} disabled={applying}>Cancel</button>
        </div>
      </Modal>
  );
}

export function StorageCapModal({
  capOpen, setCapOpen, capBusy, capInput, setCapInput, sto, capValid,
  capInRange, capOverMax, capMax,
  capWillDelete, capGb, capTyped, setCapTyped, doSaveCap,
}: {
  capOpen: boolean;
  setCapOpen: (v: boolean) => void;
  capBusy: boolean;
  capInput: string;
  setCapInput: (v: string) => void;
  sto: NvrStorage | null;
  capValid: boolean;
  /** Well-formed whole number in the absolute 0–1,000,000 GB range. */
  capInRange: boolean;
  /** Above what the disk can honour — the case this screen exists to prevent. */
  capOverMax: boolean;
  /** Largest settable cap, in GB. */
  capMax: number;
  /** True when the new cap is below current usage — footage will be erased. */
  capWillDelete: boolean;
  capGb: number | null;
  capTyped: string;
  setCapTyped: (v: string) => void;
  doSaveCap: () => void;
}) {
  const disk = sto?.disk ?? null;
  // Only meaningful when the NVR actually measured the volume; without a probe
  // capMax is the absolute bound and there is no disk figure worth quoting.
  const measured = sto?.max_limit_gb != null && disk != null;
  return (
    <Modal open={capOpen} title="Storage size cap" width={460}
        onClose={() => { if (!capBusy) setCapOpen(false); }}>
        <div className="d-hint" style={{ marginTop: 0, marginBottom: 14 }}>
          Hard limit on total recorded footage. When exceeded, the NVR deletes the <b>oldest</b> footage across
          <b> all cameras</b> (hourly) until back under the cap. Blank or 0 = uncapped (age-based retention only).
        </div>
        <div className="form-group full">
          <label>Size cap</label>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="number" min={0} max={capMax} step={1} value={capInput}
              onChange={e => setCapInput(e.target.value)} placeholder="Uncapped" style={{ maxWidth: 200 }} />
            <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>GB</span>
            {measured && (
              <button type="button" className="btn-ghost" style={{ padding: '4px 10px', fontSize: 11.5 }}
                onClick={() => setCapInput(String(capMax))} disabled={capBusy}>
                Use maximum
              </button>
            )}
          </span>
        </div>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 4 }}>
          Current usage <b style={{ fontFamily: 'var(--mono)' }}>{sto?.total_gb ?? 0} GB</b> ·{' '}
          {sto?.limit_gb ? <>current cap <b>{sto.limit_gb} GB</b></> : 'currently uncapped'}
        </div>
        {/* What the disk allows, always visible — an operator picking a number
            should not have to guess it and be corrected by an error. */}
        {measured && (
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            Maximum <b style={{ fontFamily: 'var(--mono)', color: 'var(--text)' }}>{capMax} GB</b>{' '}
            ({humanSize(disk!.free_bytes)} free on{' '}
            <span style={{ fontFamily: 'var(--mono)' }}>{disk!.path}</span> plus{' '}
            {humanSize((sto?.total_gb ?? 0) * 1024 ** 3)} already recorded, keeping{' '}
            {disk!.reserve_pct}% of the disk free).
          </div>
        )}
        {/* The maximum above is derived from free_bytes, so on a virtual disk it
            inherits that number's optimism: it is the largest cap the VM would
            accept, not the largest the host can actually back. Said here as
            well as on the panel behind this modal because this is the moment
            the number gets chosen. */}
        {measured && disk!.virtual_backing && (
          <div style={{ fontSize: 11.5, marginTop: 6, color: 'var(--yellow, #d8a83c)' }}>
            That maximum comes from a {disk!.virtual_backing.toUpperCase()} virtual disk which
            grows on demand on a smaller host drive, so it is an upper bound rather than
            space you are known to have. Pick a size the host drive can genuinely hold.
          </div>
        )}
        {!measured && (
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>
            The NVR could not measure its storage volume, so the cap is not checked
            against free space here. Set it no higher than the disk actually holds.
          </div>
        )}
        {!capInRange && (
          <div style={{ color: 'var(--red)', fontSize: 11.5, marginTop: 8 }}>
            Enter a whole number of GB between 0 and 1,000,000 (0 or blank = uncapped).
          </div>
        )}
        {/* A cap above the disk is not a bigger limit: the NVR reclaims space at
            its disk floor before the cap is ever reached, so the number would be
            saved, shown, and never enforced. Say that, rather than "invalid". */}
        {capInRange && capOverMax && (
          <div style={{ color: 'var(--red)', fontSize: 11.5, marginTop: 8, lineHeight: 1.6 }}>
            {capGb} GB is more than {disk ? disk.path : 'the storage volume'} can hold.
            The maximum is <b>{capMax} GB</b>. A larger cap would never take effect —
            the NVR starts deleting the oldest footage when the disk itself runs low,
            whatever the cap says.
          </div>
        )}
        {capWillDelete && (
          <div style={{ fontSize: 13, margin: '12px 0', lineHeight: 1.6 }}>
            <b style={{ color: 'var(--red)' }}>{capGb} GB is below current usage ({sto?.total_gb} GB).</b>{' '}
            On the next retention pass (within the hour) the oldest footage across <b>all cameras</b> is
            <b> permanently deleted</b> until usage is back under {capGb} GB. This cannot be undone.
          </div>
        )}
        {capWillDelete && (
          <div className="form-group full">
            <label>Type DELETE to confirm</label>
            <input value={capTyped} onChange={e => setCapTyped(e.target.value)} autoComplete="off" placeholder="DELETE" />
          </div>
        )}
        <div className="form-actions">
          <button className={capWillDelete ? 'btn-danger' : 'btn-primary'} onClick={doSaveCap}
            disabled={capBusy || !capValid || (capWillDelete && capTyped !== 'DELETE')}>
            {capBusy ? 'Saving…' : capGb ? `Set cap to ${capGb} GB` : 'Remove cap (uncapped)'}
          </button>
          <button className="btn-ghost" onClick={() => setCapOpen(false)} disabled={capBusy}>Cancel</button>
        </div>
      </Modal>
  );
}
