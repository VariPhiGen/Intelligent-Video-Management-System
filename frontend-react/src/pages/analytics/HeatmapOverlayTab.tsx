/**
 * HeatmapOverlayTab — where people spent their time, per camera.
 *
 * At the top, one card per camera with Heatmap Overlay configured: today's
 * heatmap (its stored 5-minute heat blocks, 00:00 to now) and the switch.
 * Below, the heatmap of any other period for one camera: pick the camera and a
 * From / To date and time, and the blocks in between are added up and drawn
 * over a recent camera frame (HeatmapRangeView). Clicking a card shows that
 * camera there. Colours are always relative to the busiest spot.
 *
 * WHAT THIS PAGE DOES NOT DO. It does not run the analytics. Heat is
 * accumulated and rendered by the analytics service for as long as a camera's
 * session runs; this page reads the latest picture about once a minute.
 * Closing it changes nothing, and opening it starts nothing.
 *
 * TWO CONFIRMATIONS, because both choices are lossy in a way an operator
 * should be told about:
 *
 *   Turning OFF   stops accumulation and freezes the picture where it is. The
 *                 configured schedule is NOT changed — that is the sentence
 *                 the dialog has to say out loud.
 *   Turning ON    with a frozen session asks whether to RESUME it (keep the
 *                 heat gathered so far) or start a NEW one (the old session
 *                 stays readable in its own right).
 */
import { useMemo, useState } from 'react';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import { ApiError } from '@/lib/api';
import { setHeatmapRuntime, type HeatmapCamera } from '@/lib/heatmaps';
import { HeatmapCard, fmt, scheduleText } from './heatmap/HeatmapCard';
import { HeatmapImage } from './heatmap/HeatmapImage';
import { HeatmapRangeView } from './heatmap/HeatmapRangeView';
import { useHeatmaps } from './heatmap/useHeatmaps';

export function HeatmapOverlayTab({ onOpenConfig }: { onOpenConfig?: () => void }) {
  const { data, error, loading, refresh } = useHeatmaps();
  const toast = useToast();
  const [confirmOff, setConfirmOff] = useState<HeatmapCamera | null>(null);
  const [confirmOn, setConfirmOn] = useState<HeatmapCamera | null>(null);
  const [full, setFull] = useState<HeatmapCamera | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);

  const cameras = useMemo(
    () => [...(data?.cameras || [])].sort((a, b) =>
      (a.name || a.slug).localeCompare(b.name || b.slug, undefined, { sensitivity: 'base' })),
    [data]);
  // First camera alphabetically until one is picked.
  const selected = cameras.find(c => c.camera_id === picked)?.camera_id ?? cameras[0]?.camera_id ?? '';

  const apply = async (cam: HeatmapCamera, enabled: boolean, mode: 'resume' | 'new') => {
    setBusy(cam.camera_id);
    try {
      await setHeatmapRuntime(cam.camera_id, enabled, { mode });
      toast(enabled
        ? (mode === 'new' ? `${cam.name}: new heatmap session started`
                          : `${cam.name}: heatmap resumed`)
        : `${cam.name}: heatmap turned off and frozen`);
      refresh();
    } catch (e: unknown) {
      toast(e instanceof ApiError ? e.message : String(e), 'err');
    } finally {
      setBusy(null);
      setConfirmOff(null);
      setConfirmOn(null);
    }
  };

  const onToggle = (cam: HeatmapCamera) => {
    if (cam.runtime_enabled) { setConfirmOff(cam); return; }
    // Nothing to resume: there is no choice to offer, so do not ask one.
    if (!cam.session || cam.session.status === 'closed') { void apply(cam, true, 'new'); return; }
    setConfirmOn(cam);
  };

  if (error?.kind === 'denied') {
    return (
      <div className="emptystate">
        <span className="glyph" aria-hidden="true">▦</span>
        <h4>Heatmaps are not available to your role</h4>
        <p>Ask an administrator for the AI analytics permission.</p>
      </div>
    );
  }

  return (
    <div className="hm-page">
      {error?.kind === 'unreachable' && (
        <div className="panel hm-warn">Heatmaps could not be read: {error.message}</div>
      )}

      {loading && !data && <div className="skel hm-skel-page" />}

      {!loading && cameras.length === 0 && (
        <div className="emptystate">
          <span className="glyph" aria-hidden="true">▦</span>
          <h4>No camera has a heatmap yet</h4>
          <p>Add <strong>Heatmap overlay</strong> to a camera in AI Config, and its heat
             will appear here while its schedule runs.</p>
          {onOpenConfig && (
            <button className="btn-primary" onClick={onOpenConfig}>Open AI Config</button>
          )}
        </div>
      )}

      {cameras.length > 0 && (
        <div className="hm-toolbar">
          <h4 className="hm-h hm-cards-title">Today, 00:00 → now</h4>
        </div>
      )}
      <div className="hm-grid">
        {cameras.map(cam => (
          <HeatmapCard key={cam.camera_id} cam={cam} busy={busy === cam.camera_id}
                       selected={cam.camera_id === selected}
                       onSelect={c => {
                         setPicked(c.camera_id);
                         document.getElementById('hm-any-period')?.scrollIntoView?.({ behavior: 'smooth' });
                       }}
                       onToggle={onToggle} onFullscreen={setFull} />
        ))}
      </div>

      {selected && (
        <>
          <h4 className="hm-h hm-cards-title" id="hm-any-period">Any other period</h4>
          <HeatmapRangeView cameras={cameras} selected={selected} onSelect={setPicked} />
        </>
      )}

      <Modal open={!!confirmOff} title="Turn off Heatmap Overlay?" width={520}
             onClose={() => setConfirmOff(null)}>
        {confirmOff && (
          <>
            <p>
              This stops heatmap accumulation for <strong>{confirmOff.name}</strong> and
              freezes the current heatmap at the current time.
            </p>
            <p className="hm-dim">
              The configured AI Config schedule is not changed
              ({scheduleText(confirmOff)}). The frozen picture stays on this page, and you
              can resume this session later.
            </p>
            <div className="form-actions">
              <button className="btn-danger" disabled={!!busy}
                      onClick={() => apply(confirmOff, false, 'resume')}>Turn Off</button>
              <button className="btn-ghost" disabled={!!busy}
                      onClick={() => setConfirmOff(null)}>Cancel</button>
            </div>
          </>
        )}
      </Modal>

      <Modal open={!!confirmOn} title="How do you want to continue?" width={560}
             onClose={() => setConfirmOn(null)}>
        {confirmOn && (
          <>
            <p>
              <strong>{confirmOn.name}</strong> has a heatmap frozen since{' '}
              {fmt(confirmOn.session?.ended_at || null)}, covering{' '}
              {fmt(confirmOn.session?.started_at || null)} onwards.
            </p>
            <div className="form-actions hm-choice">
              <button className="btn-primary" disabled={!!busy}
                      onClick={() => apply(confirmOn, true, 'resume')}>
                Resume previous session
              </button>
              <button className="btn-secondary" disabled={!!busy}
                      onClick={() => apply(confirmOn, true, 'new')}>
                Start new session
              </button>
              <button className="btn-ghost" disabled={!!busy}
                      onClick={() => setConfirmOn(null)}>Cancel</button>
            </div>
            <p className="hm-dim">
              Resuming keeps the heat gathered so far and carries on adding to it.
              A new session starts from empty; the previous one stays available.
            </p>
          </>
        )}
      </Modal>

      <Modal open={!!full} title={full ? full.name : ''} width="min(1200px, 94vw)"
             onClose={() => setFull(null)}>
        {full && (
          <>
            <HeatmapImage cameraId={full.camera_id} name={full.name}
                          alt={`Heatmap overlay for ${full.name}`}
                          dimmed={!full.runtime_enabled} />
            <p className="hm-dim">
              Today, 00:00 → now
              {' · '}{full.runtime_enabled ? 'ON' : 'OFF / FROZEN'}
            </p>
          </>
        )}
      </Modal>
    </div>
  );
}
