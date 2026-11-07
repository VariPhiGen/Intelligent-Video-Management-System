/** CameraFilterBar: chips with counts, a group with one value is hidden, clicking toggles. */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { CameraFilterBar } from './CameraFilterBar';
import type { Camera } from '@/lib/types';

const cam = (over: Partial<Camera>): Camera => ({
  id: Math.random().toString(36), name: 'Cam', slug: 'cam', enabled: true, recording: true,
  health_status: 'connected', ip: '10.0.0.1', vendor: 'CP PLUS', onvif_capable: true,
  ptz_capable: false, ptz: { supported: false }, ...over,
} as unknown as Camera);

describe('CameraFilterBar', () => {
  const cams = [cam({ ptz_capable: true, ptz: { supported: true } as never }),
                cam({ health_status: 'disconnected', onvif_capable: false, ptz: null })];

  it('shows counts, hides one-value groups, toggles a chip on and off', () => {
    const onChange = vi.fn();
    const { rerender } = render(<CameraFilterBar cameras={cams} value={{}} onChange={onChange} />);
    expect(screen.queryByText('Maker')).toBeNull();                 // one maker: nothing to choose
    expect(screen.getByText('Not checked')).toBeTruthy();
    fireEvent.click(screen.getByText('PTZ'));
    expect(onChange).toHaveBeenLastCalledWith({ type: 'ptz' });
    rerender(<CameraFilterBar cameras={cams} value={{ type: 'ptz' }} onChange={onChange} />);
    fireEvent.click(screen.getByText('PTZ'));
    expect(onChange).toHaveBeenLastCalledWith({ type: undefined });
    fireEvent.click(screen.getByText('Clear filters'));
    expect(onChange).toHaveBeenLastCalledWith({});
    expect(screen.getByText('No ONVIF login')).toBeTruthy();
  });
});
