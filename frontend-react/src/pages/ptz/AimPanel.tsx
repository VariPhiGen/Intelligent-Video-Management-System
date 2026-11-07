/**
 * AimPanel.tsx — aim a PTZ camera by DEGREES (2026-10-27): "20° left", "20° left of Home, 10° up", "30° below level".
 *
 * Two uses, one panel:
 *   • STEP — pick 5° … 90° and tap an arrow: turns that much from where the camera points now.
 *   • FROM HOME — type left/right and up/down from Home (or up/down from LEVEL) and Go; Save as preset turns that
 *     view into a named camera preset, so preset buttons, patrols and history all get it.
 *
 * Degrees come from the camera's calibration (services/ptz_calibrate: degrees per unit and where level is),
 * accurate to about ±5-10 %. Home is VMS's record of it — "Set current as home", "Learn Home", or a preset marked
 * Home — because ONVIF can't read a camera's home back. Without measured angles the panel says how to get them.
 */
import { useState } from 'react';
import { useToast } from '@/components/Toast';
import { anglesText, ptzApi, waitForSettle, type PtzAngles } from '@/lib/ptz';
import type { PtzCapabilities } from '@/lib/types';

const STEPS = [5, 10, 20, 45, 90];

export function AimPanel({ cameraId, caps, angles, homeKnown, canPtz, canManage, speed, onPresetSaved }: {
  cameraId: string;
  caps: PtzCapabilities;
  /** from the status poll — null when the calibration has no measured angles */
  angles: PtzAngles | null;
  homeKnown: boolean;
  canPtz: boolean;
  canManage: boolean;
  speed: number;
  onPresetSaved?: () => void;
}) {
  const toast = useToast();
  const [step, setStep] = useState(20);
  const [lr, setLr] = useState({ deg: '20', dir: -1 });          // -1 = left
  const [ud, setUd] = useState({ deg: '0', dir: 1, level: false }); // +1 = up
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);

  if (!caps.absolute?.pan_tilt) return null;   // degrees need "go to a position"

  async function aim(body: Parameters<typeof ptzApi.aim>[1], label: string): Promise<boolean> {
    setBusy(true);
    try {
      const r = await ptzApi.aim(cameraId, { ...body, speed });
      toast(r.clamped ? `${label} — stopped at the camera's limit` : label);
      await waitForSettle(cameraId, { panTilt: true });
      return true;
    } catch (e: any) {
      toast(e.message, 'err');
      return false;
    } finally {
      setBusy(false);
    }
  }

  const fromHome = () => ({
    right: (Number(lr.deg) || 0) * lr.dir,
    up: (Number(ud.deg) || 0) * ud.dir,
    origin: 'home' as const,
    up_from_level: ud.level,
  });
  const describe = () => {
    const b = fromHome();
    const h = b.right ? `${Math.abs(b.right)}° ${b.right > 0 ? 'right' : 'left'} of Home` : 'Home';
    const v = b.up_from_level ? (b.up ? `${Math.abs(b.up)}° ${b.up > 0 ? 'above' : 'below'} level` : 'level')
      : b.up ? `${Math.abs(b.up)}° ${b.up > 0 ? 'up' : 'down'}` : '';
    return v ? `${h}, ${v}` : h;
  };

  async function saveAsPreset() {
    if (!name.trim()) return;
    if (!(await aim(fromHome(), `Moving to ${describe()}`))) return;
    setBusy(true);
    try {
      await ptzApi.savePreset(cameraId, name.trim());
      toast(`Preset “${name.trim()}” saved at ${describe()}`);
      setName('');
      onPresetSaved?.();
    } catch (e: any) {
      toast(e.message, 'err');
    } finally {
      setBusy(false);
    }
  }

  if (!angles) {
    return (
      <div className="ptz-aim">
        <div className="ptz-aim-title">Aim by degrees</div>
        <div className="ptz-aim-note">
          Needs this camera’s angles — PTZ → Calibration → <b>Calibrate again</b> (point it at a view with ground
          below first). Then you can turn it “20° left”, or save “20° left of Home, 10° up” as a preset.
        </div>
      </div>
    );
  }

  const off = !canPtz || busy;
  return (
    <div className="ptz-aim">
      <div className="ptz-aim-title">Aim by degrees <span className="ptz-aim-now">{anglesText(angles)}</span></div>

      <div className="ptz-aim-row" role="group" aria-label="Turn by a step">
        <span className="ptz-aim-label">Step</span>
        {STEPS.map(s => (
          <button key={s} type="button" className={`ptz-zchip${step === s ? ' on' : ''}`} onClick={() => setStep(s)}>{s}°</button>
        ))}
        <span className="ptz-aim-arrows">
          <button type="button" className="ptz-aim-arrow" disabled={off} title={`${step}° left`} aria-label={`${step} degrees left`}
                  onClick={() => void aim({ right: -step, up: 0, origin: 'current' }, `Turning ${step}° left`)}>◀</button>
          <button type="button" className="ptz-aim-arrow" disabled={off} title={`${step}° up`} aria-label={`${step} degrees up`}
                  onClick={() => void aim({ right: 0, up: step, origin: 'current' }, `Turning ${step}° up`)}>▲</button>
          <button type="button" className="ptz-aim-arrow" disabled={off} title={`${step}° down`} aria-label={`${step} degrees down`}
                  onClick={() => void aim({ right: 0, up: -step, origin: 'current' }, `Turning ${step}° down`)}>▼</button>
          <button type="button" className="ptz-aim-arrow" disabled={off} title={`${step}° right`} aria-label={`${step} degrees right`}
                  onClick={() => void aim({ right: step, up: 0, origin: 'current' }, `Turning ${step}° right`)}>▶</button>
        </span>
      </div>

      <div className="ptz-aim-row" role="group" aria-label="Go to a view relative to Home">
        <span className="ptz-aim-label">From Home</span>
        <span className="ptz-aim-pair">
          <input className="ptz-aim-num" type="number" min={0} max={360} value={lr.deg} aria-label="Degrees left or right"
                 onChange={e => setLr({ ...lr, deg: e.target.value })} />°
          <select value={lr.dir} aria-label="Left or right" onChange={e => setLr({ ...lr, dir: Number(e.target.value) })}>
            <option value={-1}>left</option><option value={1}>right</option>
          </select>
        </span>
        <span className="ptz-aim-pair">
          <input className="ptz-aim-num" type="number" min={0} max={180} value={ud.deg} aria-label="Degrees up or down"
                 onChange={e => setUd({ ...ud, deg: e.target.value })} />°
          <select value={ud.dir} aria-label="Up or down" onChange={e => setUd({ ...ud, dir: Number(e.target.value) })}>
            <option value={1}>{ud.level ? 'above level' : 'up'}</option><option value={-1}>{ud.level ? 'below level' : 'down'}</option>
          </select>
        </span>
        <label className="ptz-aim-check" title="Up/down measured from level (the horizon) instead of from Home">
          <input type="checkbox" checked={ud.level} onChange={e => setUd({ ...ud, level: e.target.checked })} /> from level
        </label>
        <button type="button" className="btn-primary btn-sm" disabled={off || !homeKnown}
                onClick={() => void aim(fromHome(), `Moving to ${describe()}`)}>Go</button>
      </div>

      {!homeKnown && (
        <div className="ptz-aim-note">
          VMS doesn’t know where this camera’s Home points yet (the camera keeps it to itself).
          {canManage ? (
            <> <button type="button" className="btn-subtle btn-xs" disabled={busy}
                       onClick={async () => {
                         setBusy(true);
                         try { await ptzApi.learnHome(cameraId); toast('Home learned — the camera is at its home now'); }
                         catch (e: any) { toast(e.message, 'err'); }
                         finally { setBusy(false); }
                       }}>Learn Home</button> sends it home once and remembers where that is — or use “Set current as home”.</>
          ) : ' Ask an admin to use “Learn Home”.'}
        </div>
      )}

      {canManage && homeKnown && (
        <div className="ptz-aim-row">
          <span className="ptz-aim-label">Save as preset</span>
          <input className="ptz-aim-name" placeholder={describe()} value={name} maxLength={64}
                 onChange={e => setName(e.target.value)}
                 onKeyDown={e => { if (e.key === 'Enter') void saveAsPreset(); }} />
          <button type="button" className="btn-ghost btn-sm" disabled={off || !name.trim()}
                  title="Moves to the view above, then saves it on the camera as a named preset"
                  onClick={() => void saveAsPreset()}>Go &amp; save</button>
        </div>
      )}
      <div className="ptz-aim-note">From this camera’s calibration — about ±5–10 %, and never past the camera’s limits.</div>
    </div>
  );
}
