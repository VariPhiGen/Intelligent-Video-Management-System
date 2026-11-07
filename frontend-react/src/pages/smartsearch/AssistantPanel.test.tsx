import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { AssistantPanel } from './AssistantPanel';
import { plannerStatus } from '@/lib/searchPlanner';
import { assistantTurn, createInvestigation, restoreInvestigation, clearInvestigation, type Conversation } from '@/lib/searchAssistant';
vi.mock('@/lib/searchPlanner', () => ({ plannerStatus: vi.fn() }));
vi.mock('@/lib/searchAssistant', () => ({ assistantTurn: vi.fn(), createInvestigation: vi.fn(), restoreInvestigation: vi.fn(), clearInvestigation: vi.fn(), investigationProgress: vi.fn() }));
vi.mock('./EventClipModal', () => ({ EventClipModal: () => null }));
const empty: Conversation = { id: 'investigation', version: 0, timezone: 'UTC', exchanges: [] };
const mount = () => render(<MemoryRouter><AssistantPanel /></MemoryRouter>);
beforeEach(() => {
  vi.clearAllMocks(); sessionStorage.clear();
  vi.mocked(plannerStatus).mockResolvedValue({ enabled: false, ready: false, provider: 'qwen', assistant_ready: true });
  vi.mocked(createInvestigation).mockResolvedValue(empty);
  vi.mocked(clearInvestigation).mockResolvedValue({ ok: true });
});
it('keeps Assistant disabled until its separate readiness test passes', async () => {
  vi.mocked(plannerStatus).mockResolvedValue({ enabled: true, ready: true, provider: 'qwen', assistant_ready: false });
  mount();
  await screen.findByText(/Enable and test the Assistant/);
  expect(screen.queryByLabelText('Ask Assistant')).toBeNull();
  expect(createInvestigation).not.toHaveBeenCalled();
});
it('submits timezone, version and a stable request ID for retries', async () => {
  vi.mocked(assistantTurn).mockRejectedValueOnce(new Error('Connection interrupted')).mockResolvedValueOnce({ ...empty, version: 1 });
  const user = userEvent.setup(); mount();
  await user.type(await screen.findByLabelText('Ask Assistant'), 'count intrusion events');
  await user.click(screen.getByRole('button', { name: 'Send' }));
  await user.click(await screen.findByRole('button', { name: 'Retry this turn' }));
  await waitFor(() => expect(assistantTurn).toHaveBeenCalledTimes(2));
  expect(vi.mocked(assistantTurn).mock.calls[0]).toEqual(vi.mocked(assistantTurn).mock.calls[1]);
  expect(vi.mocked(assistantTurn).mock.calls[0][1]).toEqual(expect.objectContaining({ expected_version: 0, request_id: expect.any(String) }));
  expect(sessionStorage.getItem('smartsearch-assistant-investigation')).toBe(empty.id);
});
it('restores clarification choices and submits the selected ID directly', async () => {
  sessionStorage.setItem('smartsearch-assistant-investigation', empty.id);
  const restored: Conversation = { ...empty, version: 1, exchanges: [{ request_id: 'r1', message: 'count events at gate', answer: {
    status: 'clarification', message: 'Which camera?', choices: [{ id: 'choice-1', label: 'Gate 1' }], collections: [],
  } }] };
  vi.mocked(restoreInvestigation).mockResolvedValue(restored);
  vi.mocked(assistantTurn).mockResolvedValue({ ...restored, version: 2 });
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole('button', { name: 'Gate 1' }));
  expect(assistantTurn).toHaveBeenCalledWith(empty.id, expect.objectContaining({ choice_id: 'choice-1', expected_version: 1 }));
});
it('accepted clarification stays locked after refresh', async () => {
  sessionStorage.setItem('smartsearch-assistant-investigation', empty.id);
  vi.mocked(restoreInvestigation).mockResolvedValue({ ...empty, exchanges: [{ request_id: 'r1', message: 'count events', answer: {
    status: 'clarification', message: 'Which activity?', choices: [{ id: 'choice-1', label: 'Intrusion' }], selected_choice: 'choice-1', collections: [],
  } }] });
  mount();
  expect(await screen.findByRole('button', { name: 'Intrusion ✓' })).toBeDisabled();
});
it('renders retained event counts separately from similarity matches', async () => {
  sessionStorage.setItem('smartsearch-assistant-investigation', empty.id);
  vi.mocked(restoreInvestigation).mockResolvedValue({ ...empty, exchanges: [{ request_id: 'r1', message: 'count events', answer: {
    status: 'results', message: 'Count complete.', collections: [{ tool: 'count_events', items: [], count: 7,
      groups: [{ label: 'Gate 1', count: 7 }], scope: { time_from: '2026-10-28T00:00:00Z', time_to: '2026-10-29T00:00:00Z', timezone: 'UTC' } }],
  } }] });
  mount();
  await screen.findByText('retained AI-event records');
  expect(screen.getByRole('table')).toHaveTextContent('Gate 1');
});
it('filter chips show names, and the time window is a removable chip', async () => {
  sessionStorage.setItem('smartsearch-assistant-investigation', empty.id);
  const restored: Conversation = { ...empty, version: 3, exchanges: [{ request_id: 'r1', message: 'count intrusions at gate 1 yesterday', answer: {
    status: 'results', message: 'Count complete.', collections: [{ tool: 'count_events', items: [], count: 2,
      scope: { cameras: ['gate-1'], activities: ['restricted_zone_entry'], time_from: '2026-10-28T00:00:00Z', time_to: '2026-10-29T00:00:00Z', timezone: 'UTC' },
      scope_labels: { cameras: ['Gate 1'], activities: ['Restricted zone entry'] } }],
  } }] };
  vi.mocked(restoreInvestigation).mockResolvedValue(restored);
  vi.mocked(assistantTurn).mockResolvedValue({ ...restored, version: 4 });
  const user = userEvent.setup(); mount();
  expect(await screen.findByRole('button', { name: /Cameras: Gate 1/ })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Activities: Restricted zone entry/ })).toBeInTheDocument();
  expect(screen.queryByText(/gate-1|restricted_zone_entry/)).toBeNull();
  await user.click(screen.getByRole('button', { name: /^Time: .* ×$/ }));
  expect(assistantTurn).toHaveBeenCalledWith(empty.id, expect.objectContaining({ remove_filter: 'time', expected_version: 3 }));
});
it('chips fall back to stored values for investigations saved before labels existed', async () => {
  sessionStorage.setItem('smartsearch-assistant-investigation', empty.id);
  vi.mocked(restoreInvestigation).mockResolvedValue({ ...empty, exchanges: [{ request_id: 'r1', message: 'count events', answer: {
    status: 'results', message: '', collections: [{ tool: 'count_events', items: [], count: 1,
      scope: { cameras: ['gate-1'], time_from: '2026-10-28T00:00:00Z', time_to: '2026-10-29T00:00:00Z', timezone: 'UTC' } }],
  } }] });
  mount();
  expect(await screen.findByRole('button', { name: /Cameras: gate-1/ })).toBeInTheDocument();
});
