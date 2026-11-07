import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@/components/Toast', () => ({ useToast: () => () => {} }));
vi.mock('@/components/AuthImage', () => ({ AuthImg: () => null }));
vi.mock('@/lib/api', () => ({ apiFetch: async () => ({}), apiBlob: async () => new Blob() }));

import { PresetsPanel } from './PresetsPanel';

const mk = (i: number, extra: any = {}) => ({ token: String(i), name: `Stop ${i}`, pan: 0, tilt: 0, zoom: 0, sort_order: i, ...extra });

describe('PresetsPanel', () => {
  const base = { cameraId: 'c', cameraError: null, position: null, canPtz: true, canManage: true, speed: 0.5, reload: () => {} };

  it('pages at 24 and searches', () => {
    const presets = Array.from({ length: 30 }, (_, i) => mk(i + 1));
    render(<PresetsPanel {...base} presets={presets as any} />);
    expect(screen.getByText('1–24 of 30 presets')).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText('Search presets…'), { target: { value: 'Stop 27' } });
    expect(screen.getByText('Stop 27')).toBeTruthy();
    expect(screen.queryByText('Stop 1')).toBeNull();
  });

  it('filters to presets the camera lost', () => {
    render(<PresetsPanel {...base} presets={[mk(1), mk(2, { missing_on_camera: true })] as any} />);
    fireEvent.click(screen.getByText('Missing on camera'));
    expect(screen.queryByText('Stop 1')).toBeNull();
    expect(screen.getByText('Stop 2')).toBeTruthy();
  });

  it('updates a preset in place to the current view (same token), after asking', async () => {
    const { ptzApi } = await import('@/lib/ptz');
    const save = vi.spyOn(ptzApi, 'savePreset').mockResolvedValue({ preset_token: '2' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<PresetsPanel {...base} presets={[mk(1), mk(2)] as any} />);
    fireEvent.click(screen.getAllByText('Update')[1]);
    expect(save).toHaveBeenCalledWith('c', 'Stop 2', '2');
  });

  it('shows no Update to people who cannot manage presets', () => {
    render(<PresetsPanel {...base} canManage={false} presets={[mk(1)] as any} />);
    expect(screen.queryByText('Update')).toBeNull();
  });
});
