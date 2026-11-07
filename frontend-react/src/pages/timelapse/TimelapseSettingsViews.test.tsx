/**
 * The Time-lapse page's settings tabs: Default settings saves without the
 * per-camera-only keys and warns when the cameras won't fit the disk; Changed
 * per camera lists each changed setting against its default; the pause banner
 * says why time-lapse stopped and offers to turn it back on.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls: { path: string; init?: any }[] = [];
let answers: Record<string, any> = {};

vi.mock('@/lib/api', () => ({
  apiFetch: vi.fn(async (path: string, init?: any) => {
    calls.push({ path, init });
    const key = Object.keys(answers).find(k => path.startsWith(k));
    if (!key) throw new Error(`unexpected ${path}`);
    return typeof answers[key] === 'function' ? answers[key](init) : answers[key];
  }),
}));
vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));

import { ChangedView, DefaultsView, PauseBanner } from './TimelapseSettingsViews';

const CFG = { interval_s: 300, schedule: null, skip_dark: true, dark_threshold: 18, skip_unchanged: false,
              unchanged_threshold: 2, ptz_preset_only: null, ptz_tolerance: 0.02, resolution: '1080p',
              jpeg_quality: 80, retention_days: 90, max_storage_mb: 5120, auto_render_daily: false,
              render_fps: 24, render_overlay: true, render_deflicker: false };
const GB = 1024 ** 3;
const DEFAULTS = {
  config: CFG, builtin: CFG, bulk_max: 500, per_camera_only: ['ptz_preset_only'],
  site_only_excludes: ['ptz_preset_only', 'ptz_tolerance'], guard: { floor_free_pct: 15, budget_pct: 20 },
  estimate: { frames_per_day: 288, mb_per_day: 71, mb_total: 5120, cap_reached_after_days: 73, video_seconds_per_day: 12 },
  disk: { total_bytes: 500 * GB, free_bytes: 140 * GB, timelapse_bytes: 10 * GB, frames_bytes: 10 * GB, renders_bytes: 0,
          floor_free_pct: 15, budget_pct: 20, over: false, reason: '' },
  paused: null, enabled_cameras: 18,
};

beforeEach(() => { calls.length = 0; answers = {}; });

const wrap = (ui: JSX.Element) => render(<MemoryRouter>{ui}</MemoryRouter>);

describe('DefaultsView', () => {
  it('warns when the cameras on now would outgrow the room left, and saves without per-camera keys', async () => {
    answers = { '/timelapse/defaults': (init?: any) => (init?.method === 'PUT' ? { ...DEFAULTS, config: { ...CFG, interval_s: 600 } } : DEFAULTS),
                '/timelapse/estimate': DEFAULTS.estimate };
    wrap(<DefaultsView canManage isAdmin onChanged={() => {}} />);
    // 18 × 5 GB = 90 GB, but only min(100 - 10, 140 - 75) = 65 GB is left for time-lapse.
    expect(await screen.findByText(/More than the room left/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^Capture/), { target: { value: '600' } });
    fireEvent.click(screen.getByText('Save default settings'));
    await waitFor(() => expect(calls.some(c => c.init?.method === 'PUT')).toBe(true));
    const body = JSON.parse(calls.find(c => c.init?.method === 'PUT')!.init.body);
    expect(body.defaults.interval_s).toBe(600);
    expect('ptz_preset_only' in body.defaults || 'ptz_tolerance' in body.defaults).toBe(false);
    expect(body.guard).toEqual({ floor_free_pct: 15, budget_pct: 20 });
  });

  it('is read-only without the manage permission', async () => {
    answers = { '/timelapse/defaults': DEFAULTS, '/timelapse/estimate': DEFAULTS.estimate };
    wrap(<DefaultsView canManage={false} isAdmin={false} onChanged={() => {}} />);
    expect(await screen.findByText(/needs the “Manage time-lapse” permission/)).toBeTruthy();
    expect((screen.getByLabelText(/^Capture/) as HTMLSelectElement).disabled).toBe(true);
  });

  it('lets a time-lapse manager change the defaults but not the disk limits, which stay admin-only', async () => {
    answers = { '/timelapse/defaults': DEFAULTS,
                '/timelapse/estimate': DEFAULTS.estimate };
    wrap(<DefaultsView canManage isAdmin={false} onChanged={() => {}} />);
    expect(await screen.findByText(/Only an administrator can change these limits/)).toBeTruthy();
    expect((screen.getByLabelText(/^Capture/) as HTMLSelectElement).disabled).toBe(false);
    expect((screen.getByRole('spinbutton', { name: /free disk is below/ }) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole('spinbutton', { name: /may use at most/ }) as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/^Capture/), { target: { value: '600' } });
    fireEvent.click(screen.getByText('Save default settings'));
    await waitFor(() => expect(calls.some(c => c.init?.method === 'PUT')).toBe(true));
    const body = JSON.parse(calls.find(c => c.init?.method === 'PUT')!.init.body);
    expect('guard' in body).toBe(false);
  });
});

describe('ChangedView', () => {
  it('lists each changed setting against its default and resets one', async () => {
    answers = {
      '/timelapse/changed': { cameras: [{ id: 'c1', name: 'Tower cam', slug: 't', enabled: true, changes: {
        interval_s: { value: 60, default: 300 }, retention_days: { value: 365, default: 90 },
        ptz_preset_only: { value: '2', default: null } } }], defaults: CFG },
      '/timelapse/cameras/c1/use-defaults': {},
    };
    wrap(<ChangedView canManage canOpenCamera onChanged={() => {}} />);
    expect(await screen.findByText('Tower cam')).toBeTruthy();
    expect(screen.getByText('Every minute')).toBeTruthy();
    expect(screen.getByText('default: Every 5 minutes')).toBeTruthy();
    expect(screen.getByText('set per camera')).toBeTruthy();
    fireEvent.click(screen.getAllByText('Use default')[0]);
    await waitFor(() => expect(calls.some(c => c.path === '/timelapse/cameras/c1/use-defaults')).toBe(true));
    expect(JSON.parse(calls.find(c => c.path.endsWith('use-defaults'))!.init.body)).toEqual({ keys: ['interval_s'] });
  });

  const CHANGED = { '/timelapse/changed': { cameras: [{ id: 'c1', name: 'Tower cam', slug: 't', enabled: true,
    changes: { interval_s: { value: 60, default: 300 } } }], defaults: CFG } };

  it('opens the camera\'s own Time-lapse tab from Edit', async () => {
    answers = CHANGED;
    const Where = () => <div data-testid="where">{useLocation().pathname + useLocation().search}</div>;
    render(<MemoryRouter initialEntries={['/timelapse?tab=changed']}><Routes>
      <Route path="/timelapse" element={<ChangedView canManage canOpenCamera onChanged={() => {}} />} />
      <Route path="*" element={<Where />} />
    </Routes></MemoryRouter>);
    fireEvent.click(await screen.findByText('Edit'));
    expect(screen.getByTestId('where').textContent).toBe('/cameras/config?cam=c1&tab=timelapse');
  });

  it('hides Edit without camera configuration and the reset buttons without manage', async () => {
    answers = CHANGED;
    wrap(<ChangedView canManage={false} canOpenCamera={false} onChanged={() => {}} />);
    expect(await screen.findByText('Tower cam')).toBeTruthy();
    expect(screen.queryByText('Edit')).toBeNull();
    expect(screen.queryByText('Use defaults')).toBeNull();
  });

  it('says when every camera follows the defaults', async () => {
    answers = { '/timelapse/changed': { cameras: [], defaults: CFG } };
    wrap(<ChangedView canManage canOpenCamera onChanged={() => {}} />);
    expect(await screen.findByText(/Every camera follows the default settings/)).toBeTruthy();
  });
});

describe('PauseBanner', () => {
  it('says why and turns it back on', async () => {
    answers = { '/timelapse/guard/resume': { resumed: 3 } };
    const onChanged = vi.fn();
    wrap(<PauseBanner canManage onChanged={onChanged}
                      paused={{ at: '2026-10-17T08:20:00Z', reason: 'the server disk is 86% full', camera_ids: ['a', 'b', 'c'] }} />);
    expect(screen.getByText(/switched off on 3 cameras/)).toBeTruthy();
    expect(screen.getByText(/86% full/)).toBeTruthy();
    fireEvent.click(screen.getByText('Turn back on'));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('shows nothing when not paused', () => {
    const { container } = wrap(<PauseBanner canManage onChanged={() => {}} paused={null} />);
    expect(container.textContent).toBe('');
  });
});
