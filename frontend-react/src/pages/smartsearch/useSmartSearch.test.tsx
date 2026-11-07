import { beforeEach, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { useSmartSearch } from './useSmartSearch';
import { naturalSearch, plannerStatus } from '@/lib/searchPlanner';
import { searchPeople } from '@/lib/smartsearch';
import { ApiError } from '@/lib/api';

vi.mock('@/lib/searchPlanner', () => ({ naturalSearch: vi.fn(), plannerStatus: vi.fn() }));
vi.mock('@/lib/smartsearch', () => ({
  indexStats: vi.fn(async () => ({ domains: {}, reachable: true, configured: true })),
  searchableCameras: vi.fn(async () => ({ cameras: [], recorder_available: true })),
  summariseWithheld: vi.fn(() => ''), searchPeople: vi.fn(), searchVehicles: vi.fn(),
}));
const outcome = { results: [], withheld: {}, matched: 0, truncated: false };
const wrapper = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.mocked(plannerStatus).mockResolvedValue({ enabled: true, ready: true, provider: 'qwen' });
  vi.mocked(searchPeople).mockResolvedValue(outcome);
});
async function setup() {
  const hook = renderHook(() => useSmartSearch(), { wrapper });
  await waitFor(() => expect(hook.result.current.interpret).toBe(true));
  act(() => hook.result.current.setQuery('person yesterday'));
  return hook;
}
it('shows existing result shape and applied summary', async () => {
  vi.mocked(naturalSearch).mockResolvedValue({ status: 'results', domain: 'people', summary: 'Yesterday, UTC', outcome });
  const { result } = await setup();
  await act(() => result.current.run());
  expect(result.current.people).toEqual(outcome);
  expect(result.current.appliedSummary).toBe('Yesterday, UTC');
  expect(searchPeople).not.toHaveBeenCalled();
});
it('keeps ordinary search usable after a planner failure', async () => {
  vi.mocked(naturalSearch).mockRejectedValue(new ApiError(503, { code: 'planner_unavailable' }, 'Provider offline'));
  const { result } = await setup();
  await act(() => result.current.run());
  expect(result.current.configured).toBe(true);
  expect(result.current.reachable).toBe(true);
  expect(result.current.err).toBe('Provider offline');
  act(() => result.current.setInterpret(false));
  await act(() => result.current.run());
  expect(searchPeople).toHaveBeenCalledOnce();
  expect(result.current.people).toEqual(outcome);
});
it('shows clarification without retaining old results', async () => {
  vi.mocked(naturalSearch).mockResolvedValue({ status: 'clarification', message: 'Which camera?', choices: ['Gate', 'Yard'] });
  const { result } = await setup();
  await act(() => result.current.run());
  expect(result.current.clarification).toContain('Gate, Yard');
  expect(result.current.people).toBeNull();
});
it('ignores a late response after the operator changes the query', async () => {
  let finish!: (value: any) => void;
  vi.mocked(naturalSearch).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const { result } = await setup();
  let running!: Promise<void>;
  act(() => { running = result.current.run(); });
  act(() => result.current.setQuery('different person'));
  expect(vi.mocked(naturalSearch).mock.calls[0][1]?.aborted).toBe(true);
  await act(async () => { finish({ status: 'results', domain: 'people', summary: 'Stale', outcome }); await running; });
  expect(result.current.people).toBeNull();
  expect(result.current.appliedSummary).toBe('');
});
it('remembers an operator turning interpretation off, across visits and admin saves', async () => {
  const { result, unmount } = await setup();
  act(() => result.current.setInterpret(false));
  act(() => result.current.refreshPlanner());
  await waitFor(() => expect(plannerStatus).toHaveBeenCalledTimes(2));
  expect(result.current.interpret).toBe(false);
  unmount();
  const again = renderHook(() => useSmartSearch(), { wrapper });
  await waitFor(() => expect(again.result.current.planner?.ready).toBe(true));
  expect(again.result.current.interpret).toBe(false);
});
it('never interprets while the planner is not ready, whatever the stored choice', async () => {
  vi.mocked(plannerStatus).mockResolvedValue({ enabled: true, ready: false, provider: 'qwen' });
  const { result } = renderHook(() => useSmartSearch(), { wrapper });
  await waitFor(() => expect(result.current.planner).not.toBeNull());
  expect(result.current.interpret).toBe(false);
  act(() => result.current.setQuery('person'));
  await act(() => result.current.run());
  expect(naturalSearch).not.toHaveBeenCalled();
  expect(searchPeople).toHaveBeenCalledOnce();
});
