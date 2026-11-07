import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const auth = { me: { permissions: {} as Record<string, boolean> }, isAdmin: true };
vi.mock('@/lib/auth', () => ({ useAuth: () => auth }));
const toast = () => {};
vi.mock('@/components/Toast', () => ({ useToast: () => toast }));
vi.mock('@/extensions', () => ({ ExtensionSlot: () => null }));
vi.mock('@/pages/ptz/HistoryPanel', () => ({ HistoryPanel: () => null }));
vi.mock('./CamerasPage', () => ({ CamerasTabs: () => null }));
vi.mock('@/lib/api', () => ({ apiFetch: async () => [] }));

import { ptzApi } from '@/lib/ptz';
import { PtzPage } from './PtzPage';

const row = (id: string, extra: any = {}) => ({
  id, name: `PTZ ${id}`, slug: id, enabled: true, health_status: 'connected', position: null, presets: 2,
  presets_missing: 0, calibrated: false, calibrated_at: null, max_presets: null, ...extra,
});

describe('Cameras → PTZ bulk calibration', () => {
  beforeEach(() => { vi.restoreAllMocks(); auth.isAdmin = true; auth.me.permissions = {}; });

  it('shows each camera\'s calibration live: queued with its place, running with its step, done, failed', async () => {
    vi.spyOn(ptzApi, 'overview').mockResolvedValue({ cameras: [
      row('a', { calibration_run: { state: 'running', step: 'zoom level 3 of 8' } }),
      row('b', { calibration_run: { state: 'queued' }, queue_position: 1 }),
      row('c', { calibrated: true, calibration_levels: 8, calibration_angles: true }),
      row('d', { calibrated: true, calibration_levels: 2, calibration_run: { state: 'failed', error: 'no usable picture shift' } }),
    ] } as any);
    render(<MemoryRouter><PtzPage /></MemoryRouter>);
    expect(await screen.findByText(/Calibrating · zoom level 3 of 8/)).toBeTruthy();
    expect(screen.getByText(/Queued #1/)).toBeTruthy();
    expect(screen.getByText(/8 zoom levels · angles/)).toBeTruthy();
    expect(screen.getByText(/Last run failed: no usable picture shift/)).toBeTruthy();
  });

  it('selects the cameras not calibrated and queues them', async () => {
    vi.spyOn(ptzApi, 'overview').mockResolvedValue({ cameras: [row('a'), row('b', { calibrated: true, calibration_angles: true }), row('c')] } as any);
    const q = vi.spyOn(ptzApi, 'queueCalibration').mockResolvedValue({ added: ['a', 'c'], skipped: [] });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<MemoryRouter><PtzPage /></MemoryRouter>);
    await screen.findByText('PTZ a');
    fireEvent.click(screen.getByText('Not calibrated', { selector: 'button' }));
    fireEvent.click(screen.getByText('Calibrate 2'));
    await waitFor(() => expect(q).toHaveBeenCalledWith(['a', 'c']));
  });

  it('offers no calibration controls without "Add & manage cameras"', async () => {
    auth.isAdmin = false;
    vi.spyOn(ptzApi, 'overview').mockResolvedValue({ cameras: [row('a')] } as any);
    render(<MemoryRouter><PtzPage /></MemoryRouter>);
    await screen.findByText('PTZ a');
    expect(screen.queryByText(/^Calibrate/)).toBeNull();
  });
});
