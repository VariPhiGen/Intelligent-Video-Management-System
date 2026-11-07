/**
 * TimelapseSettingsFields.tsx — the time-lapse settings form, shared by a
 * camera's own Time-lapse tab and the Time-lapse page's Default settings tab, so
 * the two can never offer different choices for the same setting.
 *
 * With `defaults` given (the camera tab), every setting that differs from the
 * site default is marked "Changed" and names the default, so it's clear which
 * settings follow the Defaults page and which this camera holds on to.
 */
import type { CSSProperties, ReactNode } from 'react';
import { INTERVALS, WEEKDAYS, differs, isSimpleSchedule, scheduleLabel, settingText, type TimelapseConfig } from './timelapseBits';

const label: CSSProperties = { fontSize: 11.5, color: 'var(--muted)' };
const row: CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6 };
const grid = (min: number): CSSProperties => ({ display: 'grid', gridTemplateColumns: `repeat(auto-fit, minmax(${min}px, 1fr))`, gap: 'var(--s4)' });

export function TimelapseSettingsFields({ cfg, onChange, ro, defaults, privacyMasks, ptzSlot, lead }: {
  cfg: TimelapseConfig;
  onChange: (cfg: TimelapseConfig) => void;
  ro: boolean;
  /** The site defaults — marks what this camera changes. Omit on the Defaults page itself. */
  defaults?: TimelapseConfig;
  privacyMasks?: number;
  /** The PTZ preset picker (camera tab only: a preset exists on one camera). */
  ptzSlot?: ReactNode;
  /** Extra fields at the head of the first panel (the camera tab's On/Off). */
  lead?: ReactNode;
}) {
  const set = <K extends keyof TimelapseConfig>(k: K, v: TimelapseConfig[K]) => onChange({ ...cfg, [k]: v });
  // "Changed · default 5 minutes": shown only where this camera parts from the Defaults page.
  const mark = (k: keyof TimelapseConfig) => defaults && differs(cfg[k], defaults[k]) ? (
    <span className="badge badge-yellow" style={{ marginLeft: 6, fontSize: 10 }} title="Differs from the site default">
      Changed · default {settingText(k, defaults[k])}
    </span>
  ) : null;
  const simple = isSimpleSchedule(cfg.schedule);
  const rule = cfg.schedule?.rules[0];
  const setRule = (patch: Partial<{ days: number[]; start: string; end: string }>) =>
    set('schedule', { mode: 'weekly', rules: [{ days: rule?.days ?? [0, 1, 2, 3, 4, 5], start: rule?.start ?? '07:00',
                                               end: rule?.end ?? '19:00', ...patch }] });

  return (
    <>
      <div className="panel">
        <div className="panel-head"><div className="panel-head-l"><div>
          <div className="panel-title">Capture</div>
          <div className="panel-sub">Stills from the live view on a schedule, kept separately from recordings and played back as video</div>
        </div></div></div>
        <div className="panel-body" style={grid(220)}>
          {lead}
          <div style={row}>
            <label style={label} htmlFor="tl-int">Capture{mark('interval_s')}</label>
            <select id="tl-int" value={cfg.interval_s} disabled={ro} onChange={e => set('interval_s', Number(e.target.value))}>
              {INTERVALS.some(([s]) => s === cfg.interval_s) ? null : <option value={cfg.interval_s}>Every {cfg.interval_s} s</option>}
              {INTERVALS.map(([s, l]) => <option key={s} value={s}>{l}</option>)}
            </select>
          </div>
          <div style={row}>
            <label style={label} htmlFor="tl-res">Resolution{mark('resolution')}</label>
            <select id="tl-res" value={cfg.resolution} disabled={ro} onChange={e => set('resolution', e.target.value as TimelapseConfig['resolution'])}>
              <option value="1080p">1080p (default)</option>
              <option value="720p">720p: smaller files</option>
              <option value="original">Camera's own resolution</option>
            </select>
          </div>
          <div style={row}>
            <label style={label} htmlFor="tl-q">JPEG quality ({cfg.jpeg_quality}){mark('jpeg_quality')}</label>
            <input id="tl-q" type="range" min={40} max={95} value={cfg.jpeg_quality} disabled={ro}
                   onChange={e => set('jpeg_quality', Number(e.target.value))} />
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head"><div className="panel-head-l"><div>
          <div className="panel-title">When{mark('schedule')}</div>
          <div className="panel-sub">On the appliance's clock, like the recording schedule. {scheduleLabel(cfg.schedule)}</div>
        </div></div></div>
        <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s3)' }}>
          <select value={cfg.schedule ? (simple ? 'hours' : 'custom') : 'always'} disabled={ro}
                  onChange={e => set('schedule', e.target.value === 'always' ? null
                    : { mode: 'weekly', rules: [{ days: [0, 1, 2, 3, 4, 5], start: '07:00', end: '19:00' }] })}
                  aria-label="Schedule">
            <option value="hours">Set hours and days</option>
            <option value="always">Around the clock</option>
            {!simple && <option value="custom">Custom schedule (kept as is)</option>}
          </select>
          {cfg.schedule && simple && rule && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--s3)', alignItems: 'center' }}>
              <input type="time" value={rule.start} disabled={ro} aria-label="From" onChange={e => setRule({ start: e.target.value })} />
              <span style={label}>to</span>
              <input type="time" value={rule.end === '24:00' ? '23:59' : rule.end} disabled={ro} aria-label="To"
                     onChange={e => setRule({ end: e.target.value })} />
              <span style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                {WEEKDAYS.map((d, i) => (
                  <label key={d} style={{ display: 'flex', alignItems: 'center', gap: 3, fontSize: 12 }}>
                    <input type="checkbox" checked={rule.days.includes(i)} disabled={ro}
                           onChange={e => setRule({ days: e.target.checked ? [...new Set([...rule.days, i])].sort() : rule.days.filter(x => x !== i) })} />
                    {d}
                  </label>
                ))}
              </span>
            </div>
          )}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head"><div className="panel-head-l"><div>
          <div className="panel-title">Which stills to keep</div>
          <div className="panel-sub">
            {privacyMasks === undefined ? 'Privacy masks are always blurred into every still before it is stored.'
              : `Privacy masks (${privacyMasks}) are always blurred into every still before it is stored.`}
          </div>
        </div></div></div>
        <div className="panel-body" style={grid(220)}>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="checkbox" checked={cfg.skip_dark} disabled={ro} onChange={e => set('skip_dark', e.target.checked)} />
            Skip dark stills (night, lens covered){mark('skip_dark')}
          </label>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="checkbox" checked={cfg.skip_unchanged} disabled={ro} onChange={e => set('skip_unchanged', e.target.checked)} />
            Skip stills where nothing changed{mark('skip_unchanged')}
          </label>
          {ptzSlot}
          <div style={row}>
            <label style={label} htmlFor="tl-ret">Keep stills for (days){mark('retention_days')}</label>
            <input id="tl-ret" type="number" min={1} max={3650} value={cfg.retention_days} disabled={ro}
                   onChange={e => set('retention_days', Number(e.target.value))} />
          </div>
          <div style={row}>
            <label style={label} htmlFor="tl-cap">Size cap per camera (MB){mark('max_storage_mb')}</label>
            <input id="tl-cap" type="number" min={50} value={cfg.max_storage_mb} disabled={ro}
                   onChange={e => set('max_storage_mb', Number(e.target.value))} />
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head"><div className="panel-head-l"><div>
          <div className="panel-title">Videos</div>
          <div className="panel-sub">Any range can be rendered on the Time-lapse page; this makes one of yesterday every night.</div>
        </div></div></div>
        <div className="panel-body" style={grid(200)}>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="checkbox" checked={cfg.auto_render_daily} disabled={ro} onChange={e => set('auto_render_daily', e.target.checked)} />
            Make a video of each day{mark('auto_render_daily')}
          </label>
          <div style={row}>
            <label style={label} htmlFor="tl-fps">Frame rate{mark('render_fps')}</label>
            <select id="tl-fps" value={cfg.render_fps} disabled={ro} onChange={e => set('render_fps', Number(e.target.value) as 24 | 30)}>
              <option value={24}>24 fps</option><option value={30}>30 fps</option>
            </select>
          </div>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="checkbox" checked={cfg.render_overlay} disabled={ro} onChange={e => set('render_overlay', e.target.checked)} />
            Stamp camera name and time{mark('render_overlay')}
          </label>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="checkbox" checked={cfg.render_deflicker} disabled={ro} onChange={e => set('render_deflicker', e.target.checked)} />
            Smooth brightness flicker{mark('render_deflicker')}
          </label>
        </div>
      </div>
    </>
  );
}
