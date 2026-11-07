import { beforeEach, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QaSinkTab } from './QaSinkTab';
import { getQaSink, qaOutbox, saveQaSink, type QaSinkConfig } from '@/lib/qaSink';
import { listLabelClasses } from '@/lib/labelClasses';
vi.mock('@/lib/qaSink', () => ({ getQaSink: vi.fn(), qaOutbox: vi.fn(), saveQaSink: vi.fn(), retryQaEntry: vi.fn() }));
vi.mock('@/lib/labelClasses', () => ({ listLabelClasses: vi.fn() }));
const config: QaSinkConfig = {
  enabled: true, delivery_method: 'webhook', message_format: 'annotation_v1',
  webhook_url: 'https://receiver.example/annotations', webhook_token_set: true,
  bootstrap_servers: '', kafka_topic: '', payload_topic: 'vms_annotation', severity: 'low',
  sasl_mechanism: null, sasl_username: null, sasl_password_set: false, use_tls: false,
  s3_bucket: 'frames-bucket', s3_prefix: '', s3_video_prefix: null, public_base_url: null,
  aws_access_key_id_set: false, aws_secret_access_key_set: false, aws_region: null,
  aws_uses_environment: true, class_map: {}, updated_by: null, updated_at: null,
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getQaSink).mockResolvedValue(config);
  vi.mocked(qaOutbox).mockResolvedValue({ counts: { pending: 0, sent: 0, failed: 0 }, entries: [] });
  vi.mocked(listLabelClasses).mockResolvedValue([]);
  vi.mocked(saveQaSink).mockResolvedValue(config);
});
it('saves a webhook without requiring broker fields or sending an untouched token', async () => {
  const user = userEvent.setup();
  render(<QaSinkTab />);
  expect(await screen.findByLabelText('Bearer token (optional)')).toHaveValue('');
  expect(screen.queryByLabelText('Brokers')).toBeNull();
  await user.click(screen.getByText('Save', { exact: true }));
  await waitFor(() => expect(saveQaSink).toHaveBeenCalledOnce());
  const body = vi.mocked(saveQaSink).mock.calls[0][0];
  expect(body.delivery_method).toBe('webhook');
  expect(body).not.toHaveProperty('webhook_token');
  expect(body).not.toHaveProperty('webhook_token_set');
});
it('requires Kafka destination fields when switching transport', async () => {
  const user = userEvent.setup();
  render(<QaSinkTab />);
  await user.selectOptions(await screen.findByLabelText('Delivery method'), 'kafka');
  expect(screen.getByText('Save', { exact: true })).toBeDisabled();
  expect(screen.queryByLabelText('Bearer token (optional)')).toBeNull();
  expect(screen.getByText('Cannot enable without brokers, Kafka topic.')).toBeInTheDocument();
});
it('sends an explicit credential removal', async () => {
  const user = userEvent.setup();
  render(<QaSinkTab />);
  await user.click(await screen.findByText('Remove bearer token'));
  await user.click(screen.getByText('Save', { exact: true }));
  await waitFor(() => expect(saveQaSink).toHaveBeenCalledWith(expect.objectContaining({ webhook_token: '' })));
});
