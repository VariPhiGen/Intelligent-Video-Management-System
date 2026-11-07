/**
 * qaSink.ts — where operator-drawn boxes go for review off this appliance.
 *
 * Administered under Administration → Annotation Publishing. Admin-only on read as
 * well as write: the broker list, the bucket and the class map together
 * describe exactly where this site's footage is sent.
 *
 * `sasl_password` is write-only. It goes up and never comes back; the GET
 * answers `sasl_password_set` instead, which is what lets the form show "a
 * password is stored" without the value leaving the appliance. Omitting the
 * field on a save leaves the stored one alone — so editing the class map
 * cannot silently wipe the broker credential.
 */
import { apiFetch } from '@/lib/api';

export interface QaSinkConfig {
  enabled: boolean;
  delivery_method: 'kafka' | 'webhook';
  message_format: 'review_legacy' | 'annotation_v1';
  webhook_url: string | null;
  webhook_token_set: boolean;
  bootstrap_servers: string;
  /** The Kafka topic produced to. MUST be one the remote consumer subscribes
   *  to — a topic it does not subscribe to accepts every message and delivers
   *  none, with no error on either side. */
  kafka_topic: string;
  /** Carried INSIDE the message, and a different thing from `kafka_topic`.
   *  The consumer uses it to decide how to treat the event. */
  payload_topic: string;
  severity: string;
  sasl_mechanism: string | null;
  sasl_username: string | null;
  sasl_password_set: boolean;
  use_tls: boolean;
  s3_bucket: string | null;
  s3_prefix: string;
  /** Where a ±5s clip of each annotation is uploaded. Blank = no video. */
  s3_video_prefix: string | null;
  public_base_url: string | null;
  /** The sink's own S3 identity. BOTH halves are write-only — the server
   *  returns only whether each is stored, never the value, so neither can be
   *  round-tripped by the form. Unset means it signs with the appliance's
   *  environment credentials, which is the supported default, not a gap. */
  aws_access_key_id_set: boolean;
  aws_secret_access_key_set: boolean;
  aws_region: string | null;
  aws_uses_environment: boolean;
  /** label class key → the subcategory string the remote reviewers read. */
  class_map: Record<string, string>;
  updated_by: string | null;
  updated_at: string | null;
}

export type QaSinkConfigBody = Omit<
  QaSinkConfig,
  'webhook_token_set' | 'sasl_password_set' | 'aws_access_key_id_set' | 'aws_secret_access_key_set'
  | 'aws_uses_environment' | 'updated_by' | 'updated_at'
> & {
  webhook_token?: string | null;
  sasl_password?: string | null;
  /** Omit to keep the stored value; empty string to clear it. */
  aws_access_key_id?: string | null;
  aws_secret_access_key?: string | null;
};

export interface QaOutboxEntry {
  annotation_id: string;
  state: 'pending' | 'sent' | 'failed';
  attempts: number;
  last_error: string | null;
  created_at: string;
  next_attempt_at: string;
  sent_at: string | null;
}

export interface QaOutboxStatus {
  counts: { pending: number; sent: number; failed: number };
  entries: QaOutboxEntry[];
}

export const getQaSink = () => apiFetch<QaSinkConfig>('/qa-sink');

export const saveQaSink = (body: QaSinkConfigBody) =>
  apiFetch<QaSinkConfig>('/qa-sink', {
    method: 'PUT', body: JSON.stringify(body),
  });

export const qaOutbox = (state?: string) =>
  apiFetch<QaOutboxStatus>(`/qa-sink/outbox${state ? `?state=${state}` : ''}`);

export const retryQaEntry = (annotationId: string) =>
  apiFetch<{ annotation_id: string; state: string }>(
    `/qa-sink/outbox/${encodeURIComponent(annotationId)}/retry`,
    { method: 'POST' },
  );
