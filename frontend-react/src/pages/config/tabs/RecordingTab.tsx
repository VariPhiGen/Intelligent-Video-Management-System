/**
 * RecordingTab.tsx — recording mode + retention (left) and the weekly schedule
 * hour-grid (right). Modes map to what the API actually enforces:
 *   Continuous 24×7  → recording on,  no schedule
 *   Scheduled        → recording on,  weekly schedule from the grid
 *   Off — live only  → recording off
 * (No "motion-triggered" mode: motion detection is a separate, detection-only
 * feature under AI Config and does not gate recording.)
 */
import { useEffect, useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { Camera } from '@/lib/types';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import { ScheduleDays } from '../ScheduleDays';
import { SubTrackCard } from './SubTrackCard';
import { daysToSchedule, scheduleToDays, type DaySet, type WeeklySchedule } from '../schedule';
import { aiStillOn, searchIndexingActive, aiRunningPhrase } from './aiStillOn';

type Mode = 'continuous' | 'scheduled' | 'off';

const MODES: { id: Mode; label: string; hint: string }[] = [
  { id: 'continuous', label: 'Continuous 24×7', hint: 'Record whenever the camera is enabled.' },
  { id: 'scheduled', label: 'Scheduled', hint: 'Record only on the days of the month selected on the right.' },
  { id: 'off', label: 'Off — live only', hint: 'Never record; the stream stays available live.' },
];

function initialMode(c: Camera): Mode {
  if (c.recording === false) return 'off';
  return c.recording_schedule ? 'scheduled' : 'continuous';
}

export function RecordingTab({ camera, onSaved }: { camera: Camera; onSaved: () => void }) {
  const toast = useToast();
  const [mode, setMode] = useState<Mode>(() => initialMode(camera));
  const [days, setDays] = useState<DaySet>(() => scheduleToDays(camera.recording_schedule as WeeklySchedule | null));
  // Retention / groom inputs as raw text: '' = appliance default (stored NULL).
  const [retention, setRetention] = useState(() => camera.retention_days?.toString() ?? '');
  const [groom, setGroom] = useState(() => camera.groom_after_days?.toString() ?? '');
  // DPDP purpose-binding: why footage is kept this long (recorded + audited).
  const [justification, setJustification] = useState(() => camera.retention_justification ?? '');
  // Appliance groom-after default from the NVR (0 = grooming off); null until
  // loaded or when the NVR is unreachable / too old to report it.
  const [groomDefault, setGroomDefault] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  // Shorten-retention confirmation (footage deletion is irreversible).
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState('');
  // Days of existing footage the shorter window will delete; null = unknown
  // (NVR unreachable or the camera has no footage yet).
  const [affectedDays, setAffectedDays] = useState<number | null>(null);

  // Re-init on camera switch only (not the 15s refresh, so edits survive).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    setMode(initialMode(camera));
    setDays(scheduleToDays(camera.recording_schedule as WeeklySchedule | null));
    setRetention(camera.retention_days?.toString() ?? '');
    setGroom(camera.groom_after_days?.toString() ?? '');
    setJustification(camera.retention_justification ?? '');
  }, [camera.id]);

  useEffect(() => {
    let alive = true;
    apiFetch<{ groom_after_days_default?: number }>('/nvr/storage')
      .then(s => {
        if (alive && typeof s.groom_after_days_default === 'number') setGroomDefault(s.groom_after_days_default);
      })
      .catch(() => { /* NVR unreachable — placeholder falls back to generic copy */ });
    return () => { alive = false; };
  }, []);

  // 0 = reset to default; NaN when the input isn't a number at all.
  const retDays = retention.trim() === '' ? 0 : Number(retention);
  const dflt = camera.retention_default_days ?? 30;
  const currentEffective = camera.retention_days ?? dflt;
  const newEffective = retDays === 0 ? dflt : retDays;

  const groomDays = groom.trim() === '' ? 0 : Number(groom);
  const newGroomEffective = groomDays === 0 ? (groomDefault ?? 0) : groomDays;
  const curGroomEffective = camera.groom_after_days ?? groomDefault ?? 0;
  // Normalize "never grooms" (off, or footage deleted before the threshold)
  // to Infinity so threshold comparisons read naturally.
  const groomsAt = (eff: number, retentionEff: number) =>
    eff > 0 && eff < retentionEff ? eff : Infinity;
  const newGroomsAt = groomsAt(newGroomEffective, newEffective);
  const lowersRetention = newEffective < currentEffective;
  const lowersGroom = newGroomsAt < groomsAt(curGroomEffective, currentEffective);

  async function save() {
    if (mode === 'scheduled' && !days.size) {
      toast('Select at least one day to record, or choose another mode', 'err');
      return;
    }
    if (!Number.isInteger(retDays) || retDays < 0 || retDays > 3650) {
      toast('Retention must be a whole number of days between 1 and 3650, or blank for the default', 'err');
      return;
    }
    if (!Number.isInteger(groomDays) || groomDays < 0 || groomDays > 3650) {
      toast('Full-quality days must be a whole number between 1 and 3650, or blank for the default', 'err');
      return;
    }
    if (lowersRetention || lowersGroom) {
      // Estimate how much existing footage the shorter retention will delete.
      // Best-effort: 404 (no footage) and an unreachable NVR both fall back
      // to a generic warning — the prompt itself is unconditional.
      let affected: number | null = null;
      if (lowersRetention) {
        try {
          const r = await apiFetch<{ earliest: string }>(`/nvr/cameras/${encodeURIComponent(camera.slug)}/range`);
          const ageDays = (Date.now() - new Date(r.earliest).getTime()) / 86400000;
          affected = Math.max(0, Math.min(ageDays, currentEffective) - newEffective);
        } catch { /* fall through with affected = null */ }
      }
      setAffectedDays(affected);
      setTyped('');
      setConfirming(true);
      return;
    }
    await doSave();
  }

  // Recording and AI indexing are deliberately independent switches (a site
  // may index-and-alert while recording only on events), so switching
  // recording off leaves Smart Search and motion analysing this camera — and
  // storing person crops / plate reads for their own retention. An operator
  // who reads "Off — live only" as "this camera stops collecting" would be
  // wrong in a way nothing on this screen said. This banner says it, with the
  // one-click stop; keyed on the SAVED camera state, not the unsaved radio,
  // so it reflects what is actually happening right now.
  //
  // Truthfulness constraints, each one a reachable state (a privacy review
  // reads this banner as a statement of what is collected):
  //   * enabled must gate it — a DISABLED camera has its relay torn down and
  //     nothing analyses it, whatever the flags say.
  //   * an EMPTY domain set is stored as "off" by the backend, so indexing
  //     "on" with no domain selected collects nothing either.
  //   * each feature is named only when its own flag is on — AiTab's toggles
  //     are independent, so motion-only (indexing off) exists.
  //
  // The predicate lives in aiStillOn.ts because all three of those have to
  // agree with the backend's own rule (routers/cameras.py), and that agreement
  // is worth a test rather than a comment.
  const showAiBanner = aiStillOn(camera);
  const indexing = searchIndexingActive(camera);
  const [stoppingAi, setStoppingAi] = useState(false);

  async function stopAi() {
    setStoppingAi(true);
    try {
      await apiFetch(`/cameras/${camera.id}`, {
        method: 'PUT',
        body: JSON.stringify({ search_indexing: false, motion_detection: false }),
      });
      toast('AI indexing and motion detection stopped for this camera');
      onSaved();
    } catch (e: any) { toast(e.message, 'err'); }
    setStoppingAi(false);
  }

  async function doSave() {
    setSaving(true);
    try {
      // recording on/off + retention + groom-after
      await apiFetch(`/cameras/${camera.id}`, {
        method: 'PUT',
        body: JSON.stringify({ recording: mode !== 'off', retention_days: retDays, groom_after_days: groomDays, retention_justification: justification.trim() }),
      });
      // schedule — only meaningful when recording; Continuous clears it.
      if (mode === 'continuous') {
        await apiFetch(`/cameras/${camera.id}/schedule`, { method: 'PUT', body: JSON.stringify({ schedule: null }) });
      } else if (mode === 'scheduled') {
        await apiFetch(`/cameras/${camera.id}/schedule`, {
          method: 'PUT', body: JSON.stringify({ schedule: daysToSchedule(days) }),
        });
      }
      toast('Recording configuration saved');
      onSaved();
      setConfirming(false);
    } catch (e: any) { toast(e.message, 'err'); }
    setSaving(false);
  }

  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(260px, 1fr) 1.4fr', gap: 16, alignItems: 'start' }}>
        {/* Recording mode + retention */}
        <div>
          <div className="card-title" style={{ marginBottom: 12 }}>Recording mode</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {MODES.map(m => (
              <label key={m.id} style={{
                display: 'flex', gap: 10, alignItems: 'flex-start', padding: '10px 12px',
                border: `1px solid ${mode === m.id ? 'var(--accent)' : 'var(--border)'}`,
                background: mode === m.id ? 'var(--accentSoft)' : 'transparent',
                borderRadius: 8, cursor: 'pointer',
              }}>
                <input type="radio" name="rec-mode" checked={mode === m.id}
                       onChange={() => setMode(m.id)} style={{ width: 'auto', marginTop: 2 }} />
                <span>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{m.label}</div>
                  <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>{m.hint}</div>
                </span>
              </label>
            ))}
          </div>

          {showAiBanner && (
            <div style={{
              marginTop: 12, padding: '10px 12px', borderRadius: 8,
              border: '1px solid var(--yellow)', background: 'transparent',
            }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>AI is still enabled for this camera</div>
              <div style={{ fontSize: 11.5, color: 'var(--muted)', margin: '4px 0 8px' }}>
                Recording is off, but {aiRunningPhrase(camera)} on the live stream{indexing
                  ? ' — where the Smart Search index is deployed, person and vehicle crops (and'
                    + ' plate reads) keep being collected under their own retention'
                  : ''}. If stopping this camera's data collection was the intent, stop the AI too.
              </div>
              <button className="btn-primary btn-sm" disabled={stoppingAi} onClick={stopAi}>
                {stoppingAi ? 'Stopping…' : 'Stop AI for this camera'}
              </button>
              <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 8 }}>
                Already-indexed history stays searchable until it expires.
              </span>
            </div>
          )}

          <div className="card-title" style={{ margin: '20px 0 8px' }}>Retention</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'auto auto', gap: '8px 10px', alignItems: 'center', justifyContent: 'start' }}>
            <span style={{ fontSize: 12.5 }}>Keep full quality for</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="number" min={1} max={3650} step={1} value={groom}
                onChange={e => setGroom(e.target.value)}
                placeholder={groomDefault == null ? 'Appliance default'
                  : groomDefault === 0 ? 'Default: never compress' : `Default: ${groomDefault} days`}
                style={{ width: 170 }}
              />
              <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>days</span>
            </span>
            <span style={{ fontSize: 12.5 }}>Keep footage for</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="number" min={1} max={3650} step={1} value={retention}
                onChange={e => setRetention(e.target.value)}
                placeholder={`Default: ${dflt} days`}
                style={{ width: 170 }}
              />
              <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>days</span>
            </span>
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 8, lineHeight: 1.6 }}>
            {newGroomsAt !== Infinity
              ? <>Footage lifecycle: full quality for <b>{newGroomsAt} days</b> → compressed to
                 keyframe-only (plays as a slideshow) until <b>{newEffective} days</b> → deleted.</>
              : <>Footage stays full quality until it is deleted at <b>{newEffective} days</b>
                 {retention.trim() === '' && <> (the appliance default)</>}.</>}{' '}
            Blank fields use the appliance defaults. The global size cap still applies — the oldest
            footage across all cameras is evicted first when over the cap, even inside these windows.
          </div>
          <div style={{ marginTop: 14 }}>
            <label style={{ fontSize: 12.5, display: 'block', marginBottom: 6 }}>
              Retention justification <span style={{ color: 'var(--dim)' }}>(why this period — for DPDP compliance)</span>
            </label>
            <textarea
              value={justification}
              onChange={e => setJustification(e.target.value)}
              rows={2}
              maxLength={1000}
              placeholder="e.g. Employment purposes — safeguarding staff and stock; matches store policy."
              style={{ width: '100%', resize: 'vertical' }}
            />
            <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>
              Recorded against the camera and written to the audit log with who changed it and when.
            </div>
          </div>

          <SubTrackCard camera={camera} onSaved={onSaved} />
        </div>

        {/* Days-of-month schedule */}
        <div>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 12 }}>
            <span className="card-title">Recording days</span>
            {mode !== 'scheduled' && (
              <span style={{ fontSize: 11.5, color: 'var(--dim)' }}>
                {mode === 'continuous' ? 'Recording every day — switch to Scheduled to edit' : 'Recording is off'}
              </span>
            )}
          </div>
          <ScheduleDays days={days} onChange={setDays} disabled={mode !== 'scheduled'} />
        </div>
      </div>

      <div className="form-actions" style={{ marginTop: 18 }}>
        <button className="btn-primary" disabled={saving} onClick={save}>
          {saving ? 'Saving…' : 'Save configuration'}
        </button>
      </div>

      <Modal open={confirming}
             title={lowersRetention ? 'Reduce retention — footage will be deleted' : 'Compress footage sooner'}
             onClose={() => setConfirming(false)} width={480}>
        {lowersRetention && (
          <div style={{ fontSize: 13, marginBottom: 12, lineHeight: 1.6 }}>
            Shortening retention from <b>{currentEffective}</b> to <b>{newEffective}</b> days means footage
            older than {newEffective} days is permanently deleted within the hour.{' '}
            {affectedDays == null
              ? 'The amount of affected footage could not be determined right now.'
              : affectedDays < 0.1
                ? 'No stored footage is currently older than the new window, but future footage will be deleted sooner.'
                : <>Roughly <b>{affectedDays >= 10 ? Math.round(affectedDays) : affectedDays.toFixed(1)} days</b> of
                   this camera's oldest footage will be deleted.</>}{' '}
            <b>This cannot be undone</b> — there is no trash and no backup.
          </div>
        )}
        {lowersGroom && (
          <div style={{ fontSize: 13, marginBottom: 12, lineHeight: 1.6 }}>
            Full quality will now be kept for only <b>{newGroomsAt} days</b>. Older footage is
            compressed to keyframe-only during the next nightly grooming window — it stays on the
            timeline and is still searchable, but plays back as a slideshow.{' '}
            <b>The quality reduction is irreversible</b>, though no footage is deleted by this change.
          </div>
        )}
        {lowersRetention && (
          <div className="form-group full">
            <label>Type DELETE to confirm</label>
            <input value={typed} onChange={e => setTyped(e.target.value)} autoComplete="off" placeholder="DELETE" />
          </div>
        )}
        <div className="form-actions">
          <button className="btn-danger" onClick={doSave}
                  disabled={saving || (lowersRetention && typed !== 'DELETE')}>
            {saving ? 'Saving…' : lowersRetention ? `Reduce to ${newEffective} days` : 'Apply'}
          </button>
          <button className="btn-ghost" onClick={() => setConfirming(false)} disabled={saving}>Cancel</button>
        </div>
      </Modal>
    </div>
  );
}
