import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { DeepStreamTab } from './DeepStreamTab';
import { getDeepstreamSettings, listDeepstreamModels, saveDeepstreamSettings } from '@/lib/deepstream';

vi.mock('@/components/Toast', () => ({ useToast: () => vi.fn() }));
vi.mock('@/lib/deepstream', async (orig) => ({
  ...(await orig<typeof import('@/lib/deepstream')>()),
  listDeepstreamModels: vi.fn(), addDeepstreamModel: vi.fn(), restartDeepstream: vi.fn(),
  getDeepstreamSettings: vi.fn(), saveDeepstreamSettings: vi.fn(), testDeepstreamSettings: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getDeepstreamSettings).mockResolvedValue({
    api_url: null, effective_url: 'http://127.0.0.1:8081', default_url: 'http://127.0.0.1:8081',
    updated_by: null, updated_at: null,
  });
});

it('lists the models DeepStream holds and names the running one', async () => {
  vi.mocked(listDeepstreamModels).mockResolvedValue({
    active: 'plant_v2', models: [{ name: 'plant_v2', batch_size: 8 }, { name: 'school_v2', batch_size: 1 }],
  });
  render(<DeepStreamTab />);
  expect(await screen.findByText('school_v2')).toBeInTheDocument();
  expect(screen.getByText('Running:')).toBeInTheDocument();
  expect(screen.getAllByText('plant_v2')).toHaveLength(2);   // the "Running" line and its row
  expect(screen.getByRole('columnheader', { name: 'batch_size' })).toBeInTheDocument();
});

it('shows why the list is missing when the pipeline is down', async () => {
  vi.mocked(listDeepstreamModels).mockRejectedValue(new Error('DeepStream pipeline at http://127.0.0.1:8081 is unreachable'));
  render(<DeepStreamTab />);
  expect(await screen.findByText(/is unreachable/)).toBeInTheDocument();
});

it('saves a pipeline address typed into the connection panel', async () => {
  const { default: userEvent } = await import('@testing-library/user-event');
  const user = userEvent.setup();
  vi.mocked(listDeepstreamModels).mockResolvedValue({ models: [] });
  vi.mocked(getDeepstreamSettings).mockResolvedValue({
    api_url: null, effective_url: 'http://127.0.0.1:8081', default_url: 'http://127.0.0.1:8081',
    updated_by: null, updated_at: null,
  });
  vi.mocked(saveDeepstreamSettings).mockResolvedValue({
    api_url: 'http://10.0.0.5:8081', effective_url: 'http://10.0.0.5:8081', default_url: 'http://127.0.0.1:8081',
    updated_by: 'admin', updated_at: null,
  });
  render(<DeepStreamTab />);
  const field = await screen.findByLabelText('DeepStream control API address');
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();   // nothing changed yet
  await user.type(field, 'http://10.0.0.5:8081');
  await user.click(screen.getByRole('button', { name: 'Save' }));
  expect(saveDeepstreamSettings).toHaveBeenCalledWith('http://10.0.0.5:8081');
  expect(await screen.findByText(/set by admin/)).toBeInTheDocument();
});
