/**
 * The PTZ tab keeps ONE live picture across its sub-tabs: switching Control → Presets → Patrols → Calibration
 * re-lays it out without remounting (= without reconnecting the stream); History hides it unless "Show live".
 */
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useEffect } from 'react';

const mounts = { live: 0, unmounts: 0 };

vi.mock('@/pages/live/LiveVideo', () => ({
  LiveVideo: () => {
    useEffect(() => { mounts.live += 1; return () => { mounts.unmounts += 1; }; }, []);
    return <div data-testid="live" />;
  },
  PtzPosBadge: () => null,
}));
vi.mock('@/pages/live/PtzControl', () => ({ PtzClickLayer: () => null }));
vi.mock('@/components/PtzPad', () => ({ PtzPad: () => <div data-testid="pad" />, PtzSpeed: () => null }));
vi.mock('@/pages/ptz/HistoryPanel', () => ({ HistoryPanel: () => <div>history panel</div> }));
vi.mock('@/extensions', () => ({ ExtensionSlot: () => <div>patrols panel</div> }));
vi.mock('@/components/AuthImage', () => ({ AuthImg: () => null }));
const auth = { me: { permissions: {} as Record<string, boolean> }, isAdmin: true };
vi.mock('@/lib/auth', () => ({ useAuth: () => auth }));
// A STABLE toast function: a new one per render re-creates loadPresets and re-fetches forever.
const toast = () => {};
vi.mock('@/components/Toast', () => ({ useToast: () => toast }));
vi.mock('@/lib/api', () => ({
  apiFetch: async (path: string) => {
    if (path.endsWith('/ptz/presets')) return { presets: [] };
    if (path.endsWith('/ptz/status')) return { status: null, held_by: null };
    if (path.endsWith('/ptz/position')) return { position: null };
    if (path.endsWith('/ptz/calibration')) return { calibration: null, run: null };
    return {};
  },
  apiDownload: async () => {},
  apiBlob: async () => new Blob(),
}));

import { PtzTab, ptzShowsLive } from './PtzTab';

const camera = { id: 'c1', slug: 'pt1', metadata: {}, ptz: { continuous: { pan_tilt: true } } } as any;

describe('PTZ tab live picture', () => {
  beforeEach(() => { mounts.live = 0; mounts.unmounts = 0; auth.isAdmin = true; auth.me.permissions = {}; });

  it('decides from the URL when the big picture is up', () => {
    expect(ptzShowsLive(new URLSearchParams(''))).toBe(true);
    expect(ptzShowsLive(new URLSearchParams('ptz=presets'))).toBe(true);
    expect(ptzShowsLive(new URLSearchParams('ptz=calibration'))).toBe(true);
    expect(ptzShowsLive(new URLSearchParams('ptz=history'))).toBe(false);
    expect(ptzShowsLive(new URLSearchParams('ptz=history&ptzlive=1'))).toBe(true);
  });

  it('keeps one stream across Control / Presets / Patrols / Calibration and hides it on History', async () => {
    render(<MemoryRouter initialEntries={['/config?tab=ptz']}><PtzTab camera={camera} /></MemoryRouter>);
    await screen.findByTestId('live');
    for (const tab of ['Presets', 'Patrols', 'Calibration', 'Control']) {
      fireEvent.click(screen.getByRole('tab', { name: new RegExp(`^${tab}`) }));
      expect(screen.getByTestId('live')).toBeTruthy();
    }
    expect(mounts.live).toBe(1);
    expect(mounts.unmounts).toBe(0);

    fireEvent.click(screen.getByRole('tab', { name: 'History' }));
    expect(screen.queryByTestId('live')).toBeNull();
    fireEvent.click(screen.getByText('▶ Show live'));
    expect(screen.getByTestId('live')).toBeTruthy();
  });

  it('puts a small pad under the picture on the Presets sub-tab', async () => {
    render(<MemoryRouter initialEntries={['/config?tab=ptz&ptz=presets']}><PtzTab camera={camera} /></MemoryRouter>);
    await screen.findByTestId('live');
    expect(screen.getAllByTestId('pad')).toHaveLength(1);
    expect(screen.getByText(/Click the picture to centre · drag a box to zoom/)).toBeTruthy();
  });

  it('shows History only to admins and roles granted "PTZ movement history"', async () => {
    auth.isAdmin = false;
    auth.me.permissions = { ptz_control: true };
    const { unmount } = render(<MemoryRouter initialEntries={['/config?tab=ptz&ptz=history']}><PtzTab camera={camera} /></MemoryRouter>);
    await screen.findByTestId('live');                         // a history link falls back to Control
    expect(screen.queryByRole('tab', { name: 'History' })).toBeNull();
    unmount();
    auth.me.permissions = { ptz_control: true, ptz_history: true };
    render(<MemoryRouter initialEntries={['/config?tab=ptz']}><PtzTab camera={camera} /></MemoryRouter>);
    expect(await screen.findByRole('tab', { name: 'History' })).toBeTruthy();
  });
});
