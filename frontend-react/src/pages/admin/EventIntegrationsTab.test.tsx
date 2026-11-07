import { beforeEach, it, expect, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as api from '@/lib/eventIntegrations';
import { EventIntegrationsTab } from './EventIntegrationsTab';
vi.mock('@/lib/eventIntegrations', () => Object.fromEntries(['listDestinations', 'listSubscriptions', 'getControls', 'getOptions', 'destinationStatus', 'saveDestination', 'testDestination', 'listDeliveries', 'retryDelivery', 'saveControls', 'getMailServer', 'saveMailServer', 'deleteMailServer', 'fetchHostKey', 'saveSubscription', 'deleteSubscription'].map(k => [k, vi.fn()])));
const dest: api.Destination = { id: 'd1', name: 'Guard desk', kind: 'telegram', config: { chat_id: '-100' }, enabled: false, media: true, allow_insecure: false, rate_per_min: 60, max_age_s: null, has_secret: true, last_success_at: null, last_error_at: null, last_error: null };
it('saves Slack bot credentials, channel and image option', async () => {
  vi.mocked(api.listDestinations).mockResolvedValue([{ ...dest, kind: 'slack', config: {}, media: false }]);
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(await screen.findByText('Edit', { exact: true }));
  await user.selectOptions(screen.getByLabelText('Slack connection method'), 'bot');
  await user.type(screen.getByLabelText('Channel ID'), 'C0123456789');
  await user.type(screen.getByLabelText('Destination secret'), 'xoxb-test-token');
  await user.click(screen.getByLabelText('Attach event snapshot'));
  await user.click(screen.getByText('Save destination'));
  await waitFor(() => expect(api.saveDestination).toHaveBeenCalledWith('d1', expect.objectContaining({
    config: { mode: 'bot', channel_id: 'C0123456789' }, secret: 'xoxb-test-token', media: true,
  })));
});
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.listDestinations).mockResolvedValue([]);
  vi.mocked(api.listSubscriptions).mockResolvedValue([]);
  vi.mocked(api.getControls).mockResolvedValue({ enabled: true, feed_enabled: false, environment_enabled: true, public_base_url: '', environment_base_url: '', retention_days: 7 });
  vi.mocked(api.getOptions).mockResolvedValue({ cameras: [], activities: [], rules: [] });
  vi.mocked(api.destinationStatus).mockResolvedValue({ queued: 0, sent_24h: 0, failed_24h: 0 });
  vi.mocked(api.saveDestination).mockResolvedValue(dest);
  vi.mocked(api.getMailServer).mockResolvedValue({ configured: false, has_password: false });
});
it('starts empty without sending tests or enabling anything', async () => {
  render(<EventIntegrationsTab />);
  expect(await screen.findByText(/No integrations\./)).toBeInTheDocument();
  expect(api.testDestination).not.toHaveBeenCalled();
  expect(api.saveDestination).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole('tab', { name: 'Settings' }));
  expect(screen.getByLabelText('Enable pull feed for LAN systems')).not.toBeChecked();
});
it('keeps saved secrets blank and omits an untouched secret on save', async () => {
  vi.mocked(api.listDestinations).mockResolvedValue([dest]);
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  expect(await screen.findByLabelText('Enable Guard desk')).not.toBeChecked();
  await user.click(screen.getByText('Edit', { exact: true }));
  expect(screen.getByLabelText('Destination secret')).toHaveValue('');
  await user.click(screen.getByText('Save destination'));
  await waitFor(() => expect(api.saveDestination).toHaveBeenCalledOnce());
  expect(vi.mocked(api.saveDestination).mock.calls[0][1]).not.toHaveProperty('secret');
});
it('shows a test failure and does not include a live frame by default', async () => {
  vi.mocked(api.listDestinations).mockResolvedValue([dest]);
  vi.mocked(api.testDestination).mockResolvedValue({ ok: false, error: 'Telegram rejected delivery (401)' });
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(await screen.findByText('Send test'));
  expect(await screen.findByText('Telegram rejected delivery (401)')).toBeInTheDocument();
  expect(api.testDestination).toHaveBeenCalledWith('d1', false, undefined);
});
it('shows cooldown history and only offers retry for failed or expired rows', async () => {
  const base = { destination_id: 'd1', destination_name: 'Guard desk', type: 'camera.offline', camera: { id: 'c', name: 'Gate' }, attempts: 0, created_at: '2026-10-27T12:00:00Z', media: null, message_id: 'm' };
  vi.mocked(api.listDeliveries).mockResolvedValue({ items: [{ ...base, id: 1, state: 'suppressed', last_error: 'cooldown' }, { ...base, id: 2, state: 'failed', last_error: 'Connection failed' }], next: null });
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(screen.getByText('Delivery history'));
  expect(await screen.findByText('cooldown')).toBeInTheDocument();
  expect(screen.getAllByText('Retry', { exact: true })).toHaveLength(1);
});
it('saves the playback address without touching the delivery switches', async () => {
  vi.mocked(api.saveControls).mockResolvedValue({ enabled: true, feed_enabled: false, environment_enabled: true, public_base_url: 'https://vms.site.lan', environment_base_url: '', retention_days: 7 });
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(await screen.findByRole('tab', { name: 'Settings' }));
  await user.type(await screen.findByLabelText('VMS address for playback links'), 'https://vms.site.lan');
  await user.click(screen.getByText('Save address'));
  await waitFor(() => expect(api.saveControls).toHaveBeenCalledWith(true, false, 'https://vms.site.lan'));
  expect(await screen.findByText(/Open playback/)).toBeInTheDocument();
});
it('sets up the mail server without echoing a saved password', async () => {
  vi.mocked(api.saveMailServer).mockResolvedValue({ configured: true, host: 'mail.site.lan', port: 587, security: 'starttls', from_address: 'vms@site.lan', has_password: true });
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(await screen.findByRole('tab', { name: 'Settings' }));
  await user.click(await screen.findByText('Set up'));
  await user.type(screen.getByLabelText('SMTP host'), 'mail.site.lan');
  await user.type(screen.getByLabelText('Sender address'), 'vms@site.lan');
  await user.type(screen.getByLabelText('Login username (optional)'), 'vms');
  await user.type(screen.getByLabelText('SMTP password'), 'pw');
  await user.click(screen.getByText('Save mail server'));
  await waitFor(() => expect(api.saveMailServer).toHaveBeenCalledOnce());
  expect(vi.mocked(api.saveMailServer).mock.calls[0][0]).toMatchObject({ host: 'mail.site.lan', port: 587, security: 'starttls', username: 'vms', password: 'pw', from_address: 'vms@site.lan' });
  expect(await screen.findByText(/vms@site.lan via mail.site.lan:587/)).toBeInTheDocument();
});
it('creates an email destination with recipient lists and no secret', async () => {
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(await screen.findByText('Add destination'));
  await user.type(screen.getByLabelText('Destination name'), 'Ops mail');
  await user.click(screen.getByRole('radio', { name: /^Email/ }));
  expect(screen.getByText(/Set up the mail server in Settings/)).toBeInTheDocument();
  expect(screen.queryByLabelText('Destination secret')).toBeNull();
  await user.type(screen.getByLabelText('To'), 'ops@site.lan, sec@site.lan');
  await user.click(screen.getByLabelText('Attach event snapshot'));
  await user.click(screen.getByText('Save destination'));
  await waitFor(() => expect(api.saveDestination).toHaveBeenCalledOnce());
  expect(vi.mocked(api.saveDestination).mock.calls[0][1]).toMatchObject({ kind: 'email', media: true, config: { to: ['ops@site.lan', 'sec@site.lan'], cc: [], bcc: [] } });
});
it('fills the SFTP fingerprint only after the admin confirms it', async () => {
  vi.mocked(api.fetchHostKey).mockResolvedValue({ fingerprint: 'SHA256:' + 'B'.repeat(43) });
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(await screen.findByText('Add destination'));
  await user.click(screen.getByRole('radio', { name: /^SFTP export/ }));
  await user.type(screen.getByLabelText('Host'), 'nas.lan');
  const fp = screen.getByLabelText('Host key fingerprint (SHA256:…)');
  await user.click(screen.getByText('Fetch fingerprint from server'));
  await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  expect(fp).toHaveValue('');
  await user.click(screen.getByText('Fetch fingerprint from server'));
  await waitFor(() => expect(fp).toHaveValue('SHA256:' + 'B'.repeat(43)));
  expect(api.fetchHostKey).toHaveBeenCalledWith('nas.lan', 22);
  confirm.mockRestore();
});
it('says whether each destination is delivering, failing or off, and why', async () => {
  const t = (m: number) => new Date(Date.now() - m * 60000).toISOString();
  vi.mocked(api.listDestinations).mockResolvedValue([
    { ...dest, id: 'ok', name: 'Ops email', kind: 'email', config: { to: ['ops@site.lan', 'sec@site.lan'] }, enabled: true, last_success_at: t(5) },
    { ...dest, id: 'bad', name: 'Evidence NAS', kind: 'sftp', config: { host: 'nas.lan', root: '/west' }, enabled: true, last_success_at: t(60), last_error_at: t(2), last_error: 'SFTP login rejected' },
    { ...dest, id: 'off', name: 'SOC webhook', kind: 'webhook', config: { url: 'https://soc.example.com/x' }, enabled: false, last_error_at: t(1), last_error: 'old error' },
  ]);
  render(<EventIntegrationsTab />);
  const row = async (name: string) => (await screen.findByText(name)).closest('li')!;
  expect(await row('Ops email')).toHaveTextContent('Delivering');
  expect(await row('Ops email')).toHaveTextContent('Email: ops@site.lan and 1 more');
  expect(await row('Evidence NAS')).toHaveTextContent('Failing');
  expect(await row('Evidence NAS')).toHaveTextContent('SFTP login rejected');
  expect(await row('SOC webhook')).toHaveTextContent('Off');
  expect(await row('SOC webhook')).not.toHaveTextContent('old error');
});
it('warns on every tab when delivery is paused', async () => {
  vi.mocked(api.getControls).mockResolvedValue({ enabled: false, feed_enabled: false, environment_enabled: true, public_base_url: '', environment_base_url: '', retention_days: 7 });
  vi.mocked(api.listDeliveries).mockResolvedValue({ items: [], next: null });
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  expect(await screen.findByText('Nothing is being sent.')).toBeInTheDocument();
  await user.click(screen.getByRole('tab', { name: 'Delivery history' }));
  expect(screen.getByText('Nothing is being sent.')).toBeInTheDocument();
  expect(screen.getByText('Delivery paused')).toBeInTheDocument();
});
const OPTIONS = { cameras: [{ id: 'c1', name: 'Gate 1' }, { id: 'c2', name: 'Gate 2' }], activities: [{ id: 'intrusion', name: 'Intrusion' }, { id: 'loitering', name: 'Loitering' }], rules: [{ id: 'r1', name: 'After-hours entry' }] };
const subFlow = async (existing: api.Subscription[] = []) => {
  vi.mocked(api.listDestinations).mockResolvedValue([{ ...dest, kind: 'email', name: 'Ops email' }]);
  vi.mocked(api.getOptions).mockResolvedValue(OPTIONS);
  vi.mocked(api.listSubscriptions).mockResolvedValue(existing);
  vi.mocked(api.saveSubscription).mockResolvedValue({} as api.Subscription);
  const user = userEvent.setup(); render(<EventIntegrationsTab />);
  await user.click(await screen.findByRole('tab', { name: 'What to send' }));
  return user;
};
it('builds a subscription as a sentence and saves exactly what was chosen', async () => {
  const user = await subFlow();
  await user.click(await screen.findByRole('button', { name: 'Add subscription' }));
  await user.click(screen.getByLabelText(/AI detections/));
  await user.click(within(screen.getByRole('radiogroup', { name: 'Activities' })).getByLabelText('Only these'));
  await user.click(screen.getByLabelText('Intrusion'));
  await user.click(within(screen.getByRole('radiogroup', { name: 'Cameras' })).getByLabelText('Only these'));
  await user.click(screen.getByLabelText('Gate 1'));
  expect(screen.getByRole('status')).toHaveTextContent('Ops email will receive Intrusion detections and alerts from Gate 1.');
  await user.click(screen.getByRole('button', { name: 'Save subscription' }));
  await waitFor(() => expect(api.saveSubscription).toHaveBeenCalledOnce());
  expect(vi.mocked(api.saveSubscription).mock.calls[0]).toEqual([null, expect.objectContaining({
    destination_id: 'd1', name: 'Intrusion detections and alerts from Gate 1',
    types: ['alert.raised', 'ai.event.created'], filters: { activities: ['intrusion'], cameras: ['c1'] } })]);
});
it('refuses "Only these" with nothing ticked instead of widening to everything', async () => {
  const user = await subFlow();
  await user.click(await screen.findByRole('button', { name: 'Add subscription' }));
  await user.click(within(screen.getByRole('radiogroup', { name: 'Cameras' })).getByLabelText('Only these'));
  await user.click(screen.getByRole('button', { name: 'Save subscription' }));
  expect(screen.getByRole('status')).toHaveTextContent('Tick at least one camera, or choose all.');
  expect(api.saveSubscription).not.toHaveBeenCalled();
});
it('drops the filters of an event group that is unticked', async () => {
  const user = await subFlow([{ id: 's1', destination_id: 'd1', name: 'Night alerts', types: ['alert.raised'],
    filters: { rules: ['r1'], min_severity: 'critical', cameras: [] }, cooldown_s: null, enabled: true }]);
  await user.click(await screen.findByRole('button', { name: 'Edit' }));
  expect(within(screen.getByRole('radiogroup', { name: 'Cameras' })).getByLabelText('All cameras')).toBeChecked();
  await user.click(screen.getByLabelText(/Camera health/));
  await user.click(screen.getByLabelText(/^Alerts/));
  await user.click(screen.getByRole('button', { name: 'Save subscription' }));
  await waitFor(() => expect(api.saveSubscription).toHaveBeenCalledOnce());
  const [id, body] = vi.mocked(api.saveSubscription).mock.calls[0] as [string, any];
  expect(id).toBe('s1');
  expect(body).toEqual(expect.objectContaining({ name: 'Night alerts', types: ['camera.offline', 'camera.online'], filters: {} }));
  expect(body).not.toHaveProperty('destination_id');
});
