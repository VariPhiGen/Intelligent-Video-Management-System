/**
 * ChangePasswordModal.tsx — every signed-in user changes their OWN password
 * here (PUT /api/me/password), proving they know the current one.
 *
 * Distinct from Administration → Users → "Reset password", which is an admin
 * setting SOMEONE ELSE's password and needs no current password. That screen is
 * admin-only, which is why this one exists: an operator or viewer had no way to
 * rotate their own password at all.
 */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';

const MIN_LEN = 8; // matches the API's ChangePasswordBody

export function ChangePasswordModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast();
  const { me } = useAuth();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setCurrent(''); setNext(''); setConfirm(''); setErr(''); setBusy(false);
  }, [open]);

  if (!open) return null;

  // The dev bypass and the internal service key have no Keycloak account behind
  // them, so there is no password to change — say so rather than 400 on submit.
  if (me?.kind !== 'user') {
    return (
      <Modal open title="Change password" onClose={onClose} width={420}>
        <div style={{ fontSize: 13, color: 'var(--muted)' }}>
          You are signed in through the {me?.kind === 'dev' ? 'developer bypass' : 'internal service key'},
          not a user account, so there is no password to change.
        </div>
        <div className="form-actions">
          <button className="btn-ghost" onClick={onClose}>Close</button>
        </div>
      </Modal>
    );
  }

  async function submit() {
    setErr('');
    if (!current || !next) { setErr('Fill in every field'); return; }
    if (next.length < MIN_LEN) { setErr(`New password must be at least ${MIN_LEN} characters`); return; }
    if (next !== confirm) { setErr('The new passwords do not match'); return; }
    if (next === current) { setErr('The new password must differ from the current one'); return; }
    setBusy(true);
    try {
      await apiFetch('/me/password', {
        method: 'PUT',
        body: JSON.stringify({ current_password: current, new_password: next }),
      });
      toast('Password changed');
      onClose();
    } catch (e: any) {
      setBusy(false);
      setErr(e.message); // e.g. 403 "Current password is incorrect"
    }
  }

  return (
    <Modal open title="Change password" onClose={onClose} width={420}>
      <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 12 }}>
        Signed in as <strong style={{ color: 'var(--text)' }}>{me.subject}</strong>.
      </div>
      <div className="form-grid">
        <div className="form-group full">
          <label>Current password</label>
          <input type="password" autoComplete="current-password" value={current}
            onChange={e => setCurrent(e.target.value)} />
        </div>
        <div className="form-group full">
          <label>New password</label>
          <input type="password" autoComplete="new-password" value={next}
            onChange={e => setNext(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submit(); }} />
        </div>
        <div className="form-group full">
          <label>Confirm new password</label>
          <input type="password" autoComplete="new-password" value={confirm}
            onChange={e => setConfirm(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submit(); }} />
        </div>
      </div>
      <div className="d-hint" style={{ marginTop: 10 }}>
        At least {MIN_LEN} characters. Repeated wrong guesses at the current password will
        temporarily lock the account, exactly as they would on the login screen.
      </div>
      <div style={{ color: 'var(--red)', fontSize: 12, minHeight: 16, marginTop: 8 }}>{err}</div>
      <div className="form-actions">
        <button className="btn-primary" onClick={submit} disabled={busy}>Change password</button>
        <button className="btn-ghost" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}
