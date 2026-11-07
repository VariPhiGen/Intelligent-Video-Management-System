import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryInterpretationTab } from './QueryInterpretationTab';
import { plannerSettings, savePlanner, testPlanner, type PlannerSettings as Settings } from '@/lib/searchPlanner';
vi.mock('@/lib/searchPlanner', () => ({ plannerSettings: vi.fn(), savePlanner: vi.fn(), testPlanner: vi.fn() }));
const settings: Settings = {
  config: { enabled: false, provider: 'openai', profiles: {
    qwen: { model: 'local-model', base_url: 'http://local:8000/v1', reasoning_effort: 'low' },
    openai: { model: 'cloud-model', base_url: '', reasoning_effort: 'low' },
    anthropic: { model: '', base_url: '', reasoning_effort: 'low' },
  }, point_minutes: 5, lookback_hours: 24, max_window_days: 31, timeout_seconds: 20 },
  key_configured: { qwen: false, openai: true, anthropic: false }, tested: false,
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(plannerSettings).mockResolvedValue(settings);
  vi.mocked(savePlanner).mockResolvedValue(settings);
  vi.mocked(testPlanner).mockResolvedValue({ ok: true });
});
it('omits an untouched key and keeps provider profiles', async () => {
  const user = userEvent.setup();
  render(<QueryInterpretationTab />);
  expect(await screen.findByLabelText('OpenAI API key')).toHaveValue('');
  await user.click(screen.getByText('Save settings'));
  await waitFor(() => expect(savePlanner).toHaveBeenCalledWith(settings.config, {}));
});
it('sends a deliberate removal and clears the key input after saving', async () => {
  const user = userEvent.setup();
  render(<QueryInterpretationTab />);
  await user.click(await screen.findByText('Remove saved key'));
  await user.click(screen.getByText('Save settings'));
  await waitFor(() => expect(savePlanner).toHaveBeenCalledWith(settings.config, { openai: '' }));
  expect(screen.getByLabelText('OpenAI API key')).toHaveValue('');
});
it('tests a replacement without saving or activating it', async () => {
  const user = userEvent.setup();
  render(<QueryInterpretationTab />);
  await user.type(await screen.findByLabelText('OpenAI API key'), 'test-credential');
  await user.click(screen.getByText('Test connection'));
  await screen.findByText('Connection and sample interpretation passed. Save to apply these settings.');
  expect(testPlanner).toHaveBeenCalledWith(settings.config, { openai: 'test-credential' });
  expect(savePlanner).not.toHaveBeenCalled();
});
it('sends a model-default effort for models without an effort option', async () => {
  const user = userEvent.setup();
  render(<QueryInterpretationTab />);
  await user.selectOptions(await screen.findByLabelText('Reasoning effort'), 'default');
  await user.click(screen.getByText('Save settings'));
  await waitFor(() => expect(savePlanner).toHaveBeenCalledWith(expect.objectContaining({
    profiles: expect.objectContaining({ openai: { model: 'cloud-model', base_url: '', reasoning_effort: 'default' } }),
  }), {}));
});
it('shows only the required fields for each provider', async () => {
  const user = userEvent.setup();
  render(<QueryInterpretationTab />);
  const group = await screen.findByRole('radiogroup', { name: 'Provider' });
  expect(within(group).getAllByRole('radio').map(o => o.querySelector('b')?.textContent)).toEqual(['Local', 'OpenAI', 'Claude']);
  expect(within(group).getByRole('radio', { name: /OpenAI/ })).toHaveAttribute('aria-checked', 'true');
  expect(screen.queryByLabelText('Server URL')).toBeNull();
  await user.click(within(group).getByRole('radio', { name: /^Local/ }));
  expect(screen.getByLabelText('Server URL')).toHaveValue('http://local:8000/v1');
  expect(screen.getByLabelText('Model')).toHaveValue('local-model');
  expect(screen.queryByLabelText('OpenAI API key')).toBeNull();
});
it('says which features are ready, and that edits need a save first', async () => {
  vi.mocked(plannerSettings).mockResolvedValue({ ...settings, config: { ...settings.config, enabled: true }, tested: true });
  const user = userEvent.setup();
  render(<QueryInterpretationTab />);
  const badges = () => screen.getByText(/Natural-language search/, { selector: '.qi-feature' });
  await waitFor(() => expect(badges()).toHaveTextContent('Ready'));
  expect(screen.getByText(/Assistant/, { selector: '.qi-feature' })).toHaveTextContent('Off');
  expect(screen.getByText('All changes saved.')).toBeInTheDocument();
  await user.clear(screen.getByLabelText('Model'));
  await user.type(screen.getByLabelText('Model'), 'other-model');
  expect(badges()).toHaveTextContent('Save to apply');
  expect(screen.getByText('You have unsaved changes.')).toBeInTheDocument();
  await user.click(screen.getByText('Discard'));
  expect(screen.getByLabelText('Model')).toHaveValue('cloud-model');
  expect(badges()).toHaveTextContent('Ready');
});
it('turns features on with switches and saves them together', async () => {
  const user = userEvent.setup();
  render(<QueryInterpretationTab />);
  await user.click(await screen.findByRole('switch', { name: 'Enable query interpretation' }));
  await user.click(screen.getByRole('switch', { name: 'Enable Assistant' }));
  await user.click(screen.getByText('Save settings'));
  await waitFor(() => expect(savePlanner).toHaveBeenCalledWith(expect.objectContaining({ enabled: true, assistant_enabled: true }), {}));
});
