import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HealthTab } from './HealthTab';
import { apiFetch } from '@/lib/api';

vi.mock('@/lib/api', () => ({ apiFetch: vi.fn() }));

const ENGINE = {
  configured: true, reachable: true, state: 'RUNNING', cameras: 6,
  model: 'yolov8s', mode: 'auto', runtime: 'ultralytics', device: 'cpu',
  reason: 'yolov8s.pt keeps up: 9.6 fps measured vs 6.2 fps needed (x1.25 headroom)',
  capacity_fps: 9.6, demand_fps: 6.2, over_capacity: false,
  frames_processed: 38, frames_skipped_no_motion: 10725, frames_evicted: 0,
  warnings: ['The faster OpenVINO runtime cannot run on this machine, so detection uses PyTorch: probe crashed with SIGILL'],
};

function serve(engine: unknown) {
  vi.mocked(apiFetch).mockImplementation(async (path: string) => {
    if (path === '/analytics/engine') return engine as never;
    throw new Error('not under test');
  });
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })));
});
afterEach(() => vi.unstubAllGlobals());

it('shows which model runs, on what, why, and the warning an admin acts on', async () => {
  serve(ENGINE);
  render(<HealthTab />);
  expect(await screen.findByText('AI detection')).toBeInTheDocument();
  expect(screen.getByText('6 cameras · PyTorch on CPU')).toBeInTheDocument();
  expect(screen.getByText('yolov8s · chosen automatically')).toBeInTheDocument();
  expect(screen.getByText(/65% · 9.6 fps capacity/)).toBeInTheDocument();
  expect(screen.getByText(ENGINE.reason)).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('SIGILL');
});

it('flags over capacity and dropped frames instead of looking healthy', async () => {
  serve({ ...ENGINE, over_capacity: true, demand_fps: 14, frames_evicted: 120, warnings: [] });
  render(<HealthTab />);
  expect(await screen.findByText('high load')).toBeInTheDocument();
  expect(screen.getByText('Dropped (overloaded)')).toBeInTheDocument();
});

it('says the service is down when it does not answer', async () => {
  serve({ configured: true, reachable: false, warnings: ['The AI detection service is not responding.'] });
  render(<HealthTab />);
  expect(await screen.findByText('AI detection')).toBeInTheDocument();
  expect(screen.getByText('The AI detection service is not responding.')).toBeInTheDocument();
});

it('has no card where analytics is not deployed', async () => {
  serve({ configured: false, reachable: false, warnings: [] });
  render(<HealthTab />);
  await screen.findByText('vms-core');
  expect(screen.queryByText('AI detection')).toBeNull();
});
