import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const toastFn = vi.fn();
vi.mock('@/components/Toast', () => ({ useToast: () => toastFn }));

import { ptzApi, anglesText } from '@/lib/ptz';
import { AimPanel } from './AimPanel';

const caps: any = { supported: true, absolute: { pan_tilt: true, zoom: true } };
const angles = { elevation: -8.2, right_of_home: -32.4, up_from_home: 0.2 };

describe('anglesText', () => {
  it('says where it points in words', () => {
    expect(anglesText(angles)).toBe('32° left from Home · 8° below level');
    expect(anglesText({ elevation: 0.2, right_of_home: 0, up_from_home: 0 })).toBe('at Home · level');
    expect(anglesText({ elevation: 12 })).toBe('12° above level');            // Home not known
  });
});

describe('AimPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    toastFn.mockReset();
    vi.spyOn(ptzApi, 'status').mockResolvedValue({ status: { moving_pan_tilt: 'IDLE' } } as never);
  });

  it('steps by the chosen degrees from where the camera points', async () => {
    const aim = vi.spyOn(ptzApi, 'aim').mockResolvedValue({ pan: 0, tilt: 0, clamped: false });
    render(<AimPanel cameraId="c" caps={caps} angles={angles} homeKnown canPtz canManage={false} speed={0.6} />);
    fireEvent.click(screen.getByText('45°'));
    fireEvent.click(screen.getByLabelText('45 degrees left'));
    await waitFor(() => expect(aim).toHaveBeenCalledWith('c', { right: -45, up: 0, origin: 'current', speed: 0.6 }));
  });

  it('goes to a view relative to Home, up/down optionally from level', async () => {
    const aim = vi.spyOn(ptzApi, 'aim').mockResolvedValue({ pan: 0, tilt: 0, clamped: true });
    render(<AimPanel cameraId="c" caps={caps} angles={angles} homeKnown canPtz canManage={false} speed={0.5} />);
    fireEvent.change(screen.getByLabelText('Degrees up or down'), { target: { value: '30' } });
    fireEvent.change(screen.getByLabelText('Up or down'), { target: { value: '-1' } });
    fireEvent.click(screen.getByLabelText(/from level/));
    fireEvent.click(screen.getByText('Go'));
    await waitFor(() => expect(aim).toHaveBeenCalledWith('c',
      { right: -20, up: -30, origin: 'home', up_from_level: true, speed: 0.5 }));
    await waitFor(() => expect(toastFn).toHaveBeenCalledWith(expect.stringContaining("camera's limit")));
  });

  it('saves a degree view as a named preset (admins)', async () => {
    vi.spyOn(ptzApi, 'aim').mockResolvedValue({ pan: 0, tilt: 0, clamped: false });
    const save = vi.spyOn(ptzApi, 'savePreset').mockResolvedValue({ preset_token: '9' });
    const saved = vi.fn();
    render(<AimPanel cameraId="c" caps={caps} angles={angles} homeKnown canPtz canManage speed={0.5} onPresetSaved={saved} />);
    fireEvent.change(screen.getByPlaceholderText('20° left of Home'), { target: { value: 'Gate left' } });
    fireEvent.click(screen.getByText('Go & save'));
    await waitFor(() => expect(save).toHaveBeenCalledWith('c', 'Gate left'));
    expect(saved).toHaveBeenCalled();
  });

  it('offers Learn Home when VMS does not know Home, and blocks Go', () => {
    render(<AimPanel cameraId="c" caps={caps} angles={{ elevation: -3 }} homeKnown={false} canPtz canManage speed={0.5} />);
    expect(screen.getByText('Learn Home')).toBeTruthy();
    expect((screen.getByText('Go') as HTMLButtonElement).disabled).toBe(true);
  });

  it('without measured angles, says how to get them', () => {
    render(<AimPanel cameraId="c" caps={caps} angles={null} homeKnown canPtz canManage speed={0.5} />);
    expect(screen.getByText(/Calibrate again/)).toBeTruthy();
    expect(screen.queryByText('Go')).toBeNull();
  });

  it('is absent on a camera that cannot go to a position', () => {
    const { container } = render(<AimPanel cameraId="c" caps={{ supported: true } as any} angles={angles} homeKnown
                                           canPtz canManage speed={0.5} />);
    expect(container.innerHTML).toBe('');
  });
});
