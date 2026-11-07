/** The time-lapse UI's pure parts: the words an operator reads before switching
 *  it on, and the schedule shape that must survive a round trip untouched. */
import { describe, it, expect } from 'vitest';
import { bulkAttention, bulkSettingsFrom, bulkSummary, estimateLine, evenly, fmtBytes, isSimpleSchedule,
         presetOptions, scheduleLabel, statusText,
         type TimelapseSchedule, differs, diskLine, fitsBudget, settingText } from './timelapseBits';

const workday: TimelapseSchedule = { mode: 'weekly', rules: [{ days: [0, 1, 2, 3, 4, 5], start: '07:00', end: '19:00' }] };

describe('scheduleLabel', () => {
  it('spells the default as a working week', () => {
    expect(scheduleLabel(workday)).toBe('07:00–19:00, Mon–Sat');
  });
  it('null is around the clock, not "no schedule"', () => {
    expect(scheduleLabel(null)).toBe('Around the clock');
  });
  it('names scattered days one by one', () => {
    expect(scheduleLabel({ mode: 'weekly', rules: [{ days: [0, 2, 4], start: '08:00', end: '12:00' }] }))
      .toBe('08:00–12:00, Mon, Wed, Fri');
  });
  it('does not pretend a multi-rule schedule is simple', () => {
    const rich: TimelapseSchedule = { mode: 'weekly', rules: [workday.rules[0], { days: [6], start: '09:00', end: '13:00' }] };
    expect(isSimpleSchedule(rich)).toBe(false);
    expect(scheduleLabel(rich)).toContain('Custom schedule (2 rules');
  });
});

describe('estimateLine', () => {
  it('says what a day costs and what retention adds up to', () => {
    const line = estimateLine({ frames_per_day: 123, mb_per_day: 31.2, mb_total: 2808,
                                cap_reached_after_days: null, video_seconds_per_day: 5.1 }, 90);
    expect(line).toBe('≈ 123 stills a day · 31 MB a day · 5.1 s of video per day · 2.7 GB kept over 90 days');
  });
  it('warns when the size cap bites before retention', () => {
    expect(estimateLine({ frames_per_day: 8640, mb_per_day: 2000, mb_total: 5120,
                          cap_reached_after_days: 3, video_seconds_per_day: 360 }, 90))
      .toContain('once the size cap is reached after 3 days');
  });
});

describe('small helpers', () => {
  it('fmtBytes', () => {
    expect(fmtBytes(0)).toBe('0 B');
    expect(fmtBytes(2048)).toBe('2 KB');
    expect(fmtBytes(5 * 1024 ** 2)).toBe('5.0 MB');
  });
  it('statusText explains a skipped still', () => {
    expect(statusText({ result: 'dark', at: null, detail: '' })).toBe('Skipped: too dark');
    expect(statusText(null)).toBe('No capture attempted yet');
  });
  it('evenly keeps the ends', () => {
    const out = evenly(Array.from({ length: 100 }, (_, i) => i), 5);
    expect(out).toEqual([0, 25, 50, 74, 99]);
  });
});

describe('presetOptions', () => {
  const presets = [{ token: '1', name: 'Tower' }, { token: '2', name: null }];
  it('labels presets by name and token', () => {
    expect(presetOptions(presets, null)).toEqual([
      { value: '1', label: 'Tower (1)' }, { value: '2', label: '2' }]);
  });
  it('keeps a saved value the camera no longer lists, flagged', () => {
    expect(presetOptions(presets, 'Gate')[0]).toEqual({ value: 'Gate', label: 'Gate (saved; not listed by the camera)' });
  });
  it('shows the saved value plainly when presets were not read', () => {
    expect(presetOptions(null, '7')).toEqual([{ value: '7', label: '7' }]);
  });
});

describe('statusText off_preset', () => {
  it('explains a preset skip', () => {
    expect(statusText({ result: 'off_preset', at: null, detail: '' })).toBe('Skipped: camera not at its preset');
  });
});

describe('bulk switch helpers', () => {
  const base = { requested: 6, enabled: 3, updated: 0, disabled: 0, already_enabled: 1, already_disabled: 0,
                 failed: 2, changed: 3 };
  it('summarises only what happened', () => {
    expect(bulkSummary({ ...base, results: [] }, true)).toBe('3 switched on · 1 already on · 2 failed');
    expect(bulkSummary({ ...base, enabled: 0, already_enabled: 0, failed: 0, disabled: 2, results: [] }, false))
      .toBe('2 switched off');
    expect(bulkSummary({ ...base, enabled: 0, already_enabled: 0, failed: 0, results: [] }, true)).toBe('Nothing to do');
  });
  it('lists failures and noted successes, not quiet successes', () => {
    const rows = [
      { camera_id: 'a', ok: true, result: 'enabled' as const, notes: [] },
      { camera_id: 'b', ok: true, result: 'enabled' as const, notes: ['Offline right now'] },
      { camera_id: 'c', ok: false, error: 'Camera not found' },
    ];
    expect(bulkAttention({ ...base, results: rows }).map(r => r.camera_id)).toEqual(['b', 'c']);
  });
  it('the dialog edits only its own keys', () => {
    const cfg = { interval_s: 300, schedule: null, skip_dark: true, dark_threshold: 18, skip_unchanged: false,
                  unchanged_threshold: 2, ptz_preset_only: '3', ptz_tolerance: 0.02, resolution: '1080p' as const,
                  jpeg_quality: 80, retention_days: 90, max_storage_mb: 5120, auto_render_daily: false,
                  render_fps: 24 as const, render_overlay: true, render_deflicker: false };
    expect(Object.keys(bulkSettingsFrom(cfg)).sort()).toEqual(['interval_s', 'resolution', 'retention_days', 'schedule']);
  });
});

describe('site defaults and the disk guard', () => {
  const disk = { total_bytes: 1000 * 1024 ** 3, free_bytes: 300 * 1024 ** 3, timelapse_bytes: 50 * 1024 ** 3,
                 frames_bytes: 0, renders_bytes: 0, floor_free_pct: 15, budget_pct: 20, over: false, reason: '' };
  it('spells a changed setting the way the form does', () => {
    expect(settingText('interval_s', 600)).toBe('Every 10 minutes');
    expect(settingText('interval_s', 300)).toBe('Every 5 minutes');
    expect(settingText('schedule', null)).toBe('Around the clock');
    expect(settingText('max_storage_mb', 5120)).toBe('5.0 GB');
    expect(settingText('skip_dark', false)).toBe('Off');
  });
  it('compares schedules by value', () => {
    expect(differs({ mode: 'weekly', rules: [] }, { mode: 'weekly', rules: [] })).toBe(false);
    expect(differs(300, 600)).toBe(true);
  });
  it('measures a selection against the room the guard leaves', () => {
    // budget 200 GB - 50 used = 150; free 300 - 150 floor = 150 → 150 GB left
    expect(fitsBudget(5120, 18, disk)).toMatchObject({ fits: true });
    expect(fitsBudget(5120, 40, disk).fits).toBe(false);
    expect(Math.round(fitsBudget(1, 1, disk).leftBytes / 1024 ** 3)).toBe(150);
  });
  it('an unmeasured disk never warns', () => {
    expect(fitsBudget(5120, 1000, { ...disk, total_bytes: 0 }).fits).toBe(true);
    expect(diskLine({ ...disk, total_bytes: 0 })).toMatch(/Couldn't measure/);
  });
});
