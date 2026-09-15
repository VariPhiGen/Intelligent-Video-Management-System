/**
 * cameraModals.tsx — the two confirmations the inventory owns: deleting
 * cameras (optionally with their footage) and applying a bulk field change.
 *
 * Both are typed-confirm flows, and both are destructive in ways the row-level
 * UI is not — worth reading on their own rather than as 50 lines of JSX at the
 * bottom of the page.
 */
import { Modal } from '@/components/Modal';

export function DeleteCamerasModal({
  confirmingDelete, setConfirmingDelete, deleteBusy, sel, purgeFootage,
  setPurgeFootage, typedConfirm, setTypedConfirm, confirmBulkDelete,
}: {
  confirmingDelete: boolean;
  setConfirmingDelete: (v: boolean) => void;
  deleteBusy: boolean;
  /** Ids of the ticked rows. */
  sel: string[];
  purgeFootage: boolean;
  setPurgeFootage: (v: boolean) => void;
  typedConfirm: string;
  setTypedConfirm: (v: string) => void;
  confirmBulkDelete: () => void;
}) {
  return (
    <Modal open={confirmingDelete} title={`Delete ${sel.length} camera${sel.length !== 1 ? 's' : ''}`} width={480}
      onClose={() => { if (!deleteBusy) setConfirmingDelete(false); }}>
      <div style={{ fontSize: 13, marginBottom: 12 }}>
        <b>{sel.length}</b> camera{sel.length !== 1 ? 's' : ''} will be permanently removed from the registry —
        their relay streams and recording stop, and re-adding creates new identities (new slug/URL/timeline).
      </div>
      <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: 13, cursor: 'pointer' }}>
        <input type="checkbox" style={{ width: 'auto', marginTop: 2 }}
          checked={purgeFootage}
          onChange={e => { setPurgeFootage(e.target.checked); setTypedConfirm(''); }} />
        <span>Also permanently delete all recorded footage for {sel.length !== 1 ? 'these cameras' : 'this camera'}</span>
      </label>
      <div className="d-hint" style={{ margin: '4px 0 12px 26px' }}>
        {purgeFootage
          ? 'Every recorded segment is erased from disk immediately. This cannot be undone.'
          : 'Leave unchecked to keep existing footage until its retention expires.'}
      </div>
      {purgeFootage && (
        <div className="form-group full">
          <label>Type DELETE to confirm footage removal</label>
          <input value={typedConfirm} onChange={e => setTypedConfirm(e.target.value)}
            autoComplete="off" placeholder="DELETE" />
        </div>
      )}
      <div className="form-actions">
        <button className="btn-danger" onClick={confirmBulkDelete}
          disabled={deleteBusy || (purgeFootage && typedConfirm !== 'DELETE')}>
          {deleteBusy ? 'Deleting…' : purgeFootage ? 'Delete cameras + footage' : `Delete ${sel.length} camera${sel.length !== 1 ? 's' : ''}`}
        </button>
        <button className="btn-ghost" onClick={() => setConfirmingDelete(false)} disabled={deleteBusy}>Cancel</button>
      </div>
    </Modal>
  );
}

export function BulkApplyModal({
  pendingBulk, setPendingBulk, bulkBusy, runPendingBulk, sel,
}: {
  /** Which fleet-wide change is awaiting confirmation. */
  pendingBulk: 'stopRec' | 'disable' | null;
  setPendingBulk: (v: null) => void;
  bulkBusy: boolean;
  runPendingBulk: () => void;
  sel: string[];
}) {
  return (
    <Modal open={!!pendingBulk} width={440}
      title={pendingBulk === 'disable' ? 'Disable cameras' : 'Stop recording'}
      onClose={() => { if (!bulkBusy) setPendingBulk(null); }}>
      <div style={{ fontSize: 13, marginBottom: 14, lineHeight: 1.6 }}>
        {pendingBulk === 'disable'
          ? <>Disable <b>{sel.length}</b> camera{sel.length !== 1 ? 's' : ''}? Their live streams and recording
              stop immediately — existing footage is kept, and you can re-enable them anytime.</>
          : <>Stop recording on <b>{sel.length}</b> camera{sel.length !== 1 ? 's' : ''}? They stay live but
              capture no new footage. Existing footage is kept.</>}
      </div>
      <div className="form-actions">
        <button className="btn-danger" onClick={runPendingBulk} disabled={bulkBusy}>
          {bulkBusy ? 'Working…' : pendingBulk === 'disable' ? `Disable ${sel.length}` : `Stop recording on ${sel.length}`}
        </button>
        <button className="btn-ghost" onClick={() => setPendingBulk(null)} disabled={bulkBusy}>Cancel</button>
      </div>
    </Modal>
  );
}
