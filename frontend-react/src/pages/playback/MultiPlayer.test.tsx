import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import { MultiPlayer } from './MultiPlayer';

const mocks = vi.hoisted(() => ({
  toast: vi.fn(),
  players: [] as Array<{ emit: (event: string, data: unknown) => void }>,
}));

vi.mock('hls.js', () => ({
  default: class {
    static isSupported = () => true;
    static Events = { LEVEL_LOADED: 'level', ERROR: 'error' };
    handlers: Record<string, (event: string, data: unknown) => void> = {};
    constructor() { mocks.players.push(this); }
    on(event: string, handler: (event: string, data: unknown) => void) { this.handlers[event] = handler; }
    emit(event: string, data: unknown) { this.handlers[event]?.(event, data); }
    loadSource() {}
    attachMedia(video: HTMLVideoElement) {
      // Match hls.js's ManagedMediaSource path: video.src remains empty.
      const source = document.createElement('source');
      source.src = 'blob:managed-media-source';
      video.appendChild(source);
    }
    destroy() {}
  },
}));
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ me: { permissions: {} } }),
  authHeaders: async () => ({}),
}));
vi.mock('@/components/Toast', () => ({ useToast: () => mocks.toast }));
vi.mock('./Timeline', () => ({ Timeline: () => null }));
vi.mock('./CameraRail', () => ({ CameraRail: () => null }));
vi.mock('./timelineMarks', () => ({
  cameraHasAi: () => false, indexedKinds: () => [],
  useLaneMarks: () => ({}), clipToFootage: () => [],
}));
vi.mock('./coverage', async importOriginal => ({
  ...await importOriginal<typeof import('./coverage')>(),
  nvrCoverage: async () => null,
}));

beforeEach(() => {
  mocks.players.length = 0;
  vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, 'canPlayType').mockReturnValue('');
});

async function mountPlayers() {
  const cams = ['camera-a', 'camera-b'].map(name => ({
    name, recording: true, storage_bytes: 1000,
    earliest: '2026-10-26T04:00:00Z', latest: '2026-10-26T05:00:00Z',
  }));
  const { container } = render(<MultiPlayer nvrCams={cams} registry={[]} />);
  await waitFor(() => expect(mocks.players).toHaveLength(2));
  return [...container.querySelectorAll('video')];
}

it('clears loading when a source-child video becomes ready and restores only buffering on a stall', async () => {
  const videos = await mountPlayers();
  expect(screen.getAllByText('Loading…')).toHaveLength(2);
  expect(videos[0].src).toBe('');
  expect(videos[0].querySelector('source')).not.toBeNull();
  fireEvent.canPlay(videos[0]);
  expect(screen.getAllByText('Loading…')).toHaveLength(1);
  fireEvent.playing(videos[1]);
  expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
  fireEvent.waiting(videos[0]);
  expect(document.querySelectorAll('.spinner')).toHaveLength(1);
  expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
  fireEvent.playing(videos[0]);
  expect(document.querySelectorAll('.spinner')).toHaveLength(0);
});

it('shows missing footage for one camera while another camera becomes ready', async () => {
  const videos = await mountPlayers();
  act(() => mocks.players[0].emit('error', {
    fatal: true, response: { code: 404, data: JSON.stringify({ detail: {
      error: 'No footage in requested window',
      recorded_range: { earliest: '2026-10-26T04:00:00Z', latest: '2026-10-26T04:10:00Z' },
    } }) },
  }));
  fireEvent.canPlay(videos[1]);
  expect(screen.getByText('No recording at this time')).toBeInTheDocument();
  expect(screen.queryByText('Loading…')).not.toBeInTheDocument();
});
