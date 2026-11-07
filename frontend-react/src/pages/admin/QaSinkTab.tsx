/**
 * QaSinkTab — where operator-drawn boxes go for review off this appliance.
 *
 * THE SCREEN HAS ONE JOB BEYOND COLLECTING FIELDS: making two things true that
 * are not obvious from the field names.
 *
 * 1. THE TWO TOPICS ARE DIFFERENT THINGS. "Kafka topic" is what this appliance
 *    produces to and must be a topic the remote consumer subscribes to.
 *    "Payload topic" is a string carried inside the message that the consumer
 *    reads to decide how to treat the event. Setting one to the other's value
 *    produces a sink that reports success and delivers nothing, with no error
 *    anywhere — so they are separated visually and each says what it is for.
 *
 * 2. PUBLISHING IS NOT REVERSIBLE. A frame that reaches the reviewer's system
 *    is a copy this appliance can no longer erase. That sentence belongs on
 *    the screen where the decision is made, not in a document.
 *
 * The class map is rendered from the live label taxonomy rather than as free
 * text pairs. A key that does not match a real class is a mapping that can
 * never fire, and letting one be typed is letting a silent no-op be saved.
 *
 * The password field is write-only, matching the API: it is never populated
 * from the server, and leaving it untouched leaves the stored one alone. A
 * form that cleared the credential whenever somebody edited the class map
 * would be the kind of bug nobody finds until the relay stops.
 */
import { useEffect, useMemo, useState } from 'react';
import { useToast } from '@/components/Toast';
import { listLabelClasses, type LabelClass } from '@/lib/labelClasses';
import {
  getQaSink, qaOutbox, retryQaEntry, saveQaSink,
  type QaOutboxStatus, type QaSinkConfig,
} from '@/lib/qaSink';

const BLANK: QaSinkConfig = {
  delivery_method: 'kafka', message_format: 'review_legacy', webhook_url: null, webhook_token_set: false,
  enabled: false, bootstrap_servers: '', kafka_topic: '',
  payload_topic: 'vms_annotation', severity: 'low',
  sasl_mechanism: null, sasl_username: null, sasl_password_set: false,
  use_tls: false, s3_bucket: null, s3_prefix: '', s3_video_prefix: null,
  public_base_url: null,
  aws_access_key_id_set: false, aws_secret_access_key_set: false,
  aws_region: null, aws_uses_environment: true,
  class_map: {}, updated_by: null, updated_at: null,
};

const MECHANISMS = ['PLAIN', 'SCRAM-SHA-256', 'SCRAM-SHA-512'] as const;

export function QaSinkTab() {
  const toast = useToast();
  const [cfg, setCfg] = useState<QaSinkConfig | null>(null);
  const [classes, setClasses] = useState<LabelClass[]>([]);
  const [outbox, setOutbox] = useState<QaOutboxStatus | null>(null);
  // Separate from `cfg` on purpose: the server never sends it back, so it has
  // no business living in the object that mirrors the server's state.
  const [password, setPassword] = useState<string | null>(null);
  // Both halves of the S3 credential are write-only, so neither lives in
  // `cfg` — that object mirrors what the server will send back, and the
  // server sends back neither.
  const [awsKeyId, setAwsKeyId] = useState<string | null>(null);
  const [awsSecret, setAwsSecret] = useState<string | null>(null);
  const [webhookToken, setWebhookToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  async function reload() {
    try {
      const [config, taxonomy, queue] = await Promise.all([
        getQaSink(), listLabelClasses(), qaOutbox(),
      ]);
      setCfg(config);
      setClasses(taxonomy);
      setOutbox(queue);
      setPassword(null);
      setWebhookToken(null);
      setAwsKeyId(null);
      setAwsSecret(null);
      setErr('');
    } catch (e: any) {
      setErr(e?.message || 'Could not load annotation publishing settings');
    }
  }

  useEffect(() => { void reload(); }, []);

  const set = (patch: Partial<QaSinkConfig>) =>
    setCfg(c => (c ? { ...c, ...patch } : c));

  /** The server's own rule, restated so "enable" fails here rather than in a
   *  422 — and so the reason is attached to the checkbox that caused it. */
  const incomplete = useMemo(() => {
    if (!cfg) return [];
    return ([
      ...(cfg.delivery_method === 'webhook'
        ? [['Webhook URL', cfg.webhook_url || '']]
        : [['brokers', cfg.bootstrap_servers], ['Kafka topic', cfg.kafka_topic]]),
      ['S3 bucket', cfg.s3_bucket || ''],
    ] as [string, string][]).filter(([, v]) => !v.trim()).map(([n]) => n);
  }, [cfg]);

  const blocked = cfg?.enabled && incomplete.length > 0;

  async function act(fn: () => Promise<unknown>, ok: string) {
    setBusy(true);
    try {
      await fn();
      toast(ok);
      await reload();
    } catch (e: any) {
      toast(e?.message || 'That did not work', 'err');
    } finally {
      setBusy(false);
    }
  }

  const save = () => act(async () => {
    if (!cfg) return;
    const {
      webhook_token_set: _w, sasl_password_set: _s, aws_access_key_id_set: _i,
      aws_secret_access_key_set: _k, aws_uses_environment: _e,
      updated_by: _b, updated_at: _a, ...body
    } = cfg;
    await saveQaSink({
      ...body,
      ...(webhookToken === null ? {} : { webhook_token: webhookToken }),
      // Omitted means "leave the stored one alone". Only send either secret
      // when the administrator actually typed into its field.
      ...(password === null ? {} : { sasl_password: password }),
      ...(awsKeyId === null ? {} : { aws_access_key_id: awsKeyId }),
      ...(awsSecret === null ? {} : { aws_secret_access_key: awsSecret }),
    });
  }, 'Annotation publishing settings saved');

  if (err) return <div className="panel"><div className="panel-body"><div className="empty">{err}</div></div></div>;
  if (!cfg) {
    return (
      <div className="panel"><div className="panel-body"><div className="empty">
        <span className="spinner" /><span style={{ marginLeft: 8 }}>Loading…</span>
      </div></div></div>
    );
  }

  const active = classes.filter(c => c.active);

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <div className="card-title">Annotation Publishing</div>
          <button className="btn-primary btn-sm" disabled={busy || !!blocked}
                  onClick={save}>Save</button>
        </div>
        <div className="panel-body">
          <div className="d-hint" style={{ marginBottom: 'var(--s4)' }}>
            Send annotations drawn on Playback and their supporting media to an
            external system. Media is uploaded to object storage with public URLs;
            annotation messages use the selected delivery method. Published annotations
            are retained beyond the usual annotation deadline.
            <strong> A frame that has been published cannot be withdrawn from the
            receiving system by an erasure request raised on this appliance.</strong>
          </div>

          <div className="form-grid" style={{ marginBottom: 'var(--s4)' }}>
            <label>Delivery method
              <select value={cfg.delivery_method} disabled={busy}
                onChange={e => set({ delivery_method: e.target.value as QaSinkConfig['delivery_method'] })}>
                <option value="kafka">Kafka</option>
                <option value="webhook">HTTP webhook / API</option>
              </select>
            </label>
            <label>Message format
              <select value={cfg.message_format} disabled={busy}
                onChange={e => set({ message_format: e.target.value as QaSinkConfig['message_format'] })}>
                <option value="review_legacy">Existing review integration</option>
                <option value="annotation_v1">Annotation JSON v1</option>
              </select>
              <span className="d-hint">Use Annotation JSON v1 for new integrations. Keep the existing format for current review consumers.</span>
            </label>
            {cfg.delivery_method === 'webhook' && <>
              <label>Webhook URL
                <input value={cfg.webhook_url || ''} disabled={busy} placeholder="https://example.com/api/annotations"
                  onChange={e => set({ webhook_url: e.target.value || null })} />
              </label>
              <label>Bearer token (optional)
                <input type="password" autoComplete="new-password" value={webhookToken ?? ''} disabled={busy}
                  placeholder={cfg.webhook_token_set ? 'Configured — leave untouched to retain' : 'No token configured'}
                  onChange={e => setWebhookToken(e.target.value)} />
              </label>
              {cfg.webhook_token_set && <button disabled={busy} className="btn-sm" onClick={() => setWebhookToken('')}>
                {webhookToken === '' ? 'Token will be removed on save' : 'Remove bearer token'}
              </button>}
              <div className="d-hint">Sends an HTTPS POST. A successful response confirms acceptance by the destination.</div>
            </>}
          </div>
          <div className="d-hint">Changes apply to new annotations. Queued deliveries retain their original destination and credentials. Disabling publishing pauses the queue.</div>

          <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input type="checkbox" checked={cfg.enabled}
                   onChange={e => set({ enabled: e.target.checked })} />
            Publish new annotations to an external system
          </label>
          {blocked && (
            <div className="d-hint" style={{ color: 'var(--danger)', marginTop: 6 }}>
              Cannot enable without {incomplete.join(', ')}.
            </div>
          )}

          <div className="form-grid" style={{ marginTop: 'var(--s4)' }}>
            {cfg.delivery_method === 'kafka' && <>
            <label>
              Brokers
              <input value={cfg.bootstrap_servers} disabled={busy}
                     placeholder="host:9092, host2:9092"
                     onChange={e => set({ bootstrap_servers: e.target.value })} />
              <span className="d-hint">Comma-separated.</span>
            </label>

            <label>
              Kafka topic
              <input value={cfg.kafka_topic} disabled={busy}
                     placeholder="annotation-events"
                     onChange={e => set({ kafka_topic: e.target.value })} />
              <span className="d-hint">
                Must be a topic the receiving system already consumes. A topic it
                does not subscribe to accepts every message and delivers none.
              </span>
            </label>

            </>}
            {cfg.message_format === 'review_legacy' && <label>
              Payload topic
              <input value={cfg.payload_topic} disabled={busy}
                     onChange={e => set({ payload_topic: e.target.value })} />
              <span className="d-hint">
                A tag carried inside the message, separate from the Kafka topic. Keep it
                distinct from the receiving system's own event topics so operator
                boxes go straight to human review instead of being re-checked by
                its model.
              </span>
            </label>

            }
            <label>
              Severity
              <input value={cfg.severity} disabled={busy}
                     onChange={e => set({ severity: e.target.value })} />
            </label>

            <label>
              S3 bucket
              <input value={cfg.s3_bucket || ''} disabled={busy}
                     onChange={e => set({ s3_bucket: e.target.value || null })} />
              <span className="d-hint">
                Published frames are written with public read. Scope the bucket
                policy to the prefix below and nothing else.
              </span>
            </label>

            <label>
              S3 prefix
              <input value={cfg.s3_prefix} disabled={busy}
                     placeholder="annotations/"
                     onChange={e => set({ s3_prefix: e.target.value })} />
            </label>

            <label>
              S3 video prefix
              <input value={cfg.s3_video_prefix || ''} disabled={busy}
                     placeholder="annotation-videos/"
                     onChange={e => set({ s3_video_prefix: e.target.value || null })} />
              <span className="d-hint">
                Optional. When set, a clip from 5 seconds before to 5 seconds
                after each annotation is uploaded here with public read, and
                its full URL is sent as the video. Leave blank to send no video.
                If the NVR has no footage for that moment the box is still
                published, without a video.
              </span>
            </label>

            <label>
              Public base URL
              <input value={cfg.public_base_url || ''} disabled={busy}
                     placeholder="https://cdn.example.com"
                     onChange={e => set({ public_base_url: e.target.value || null })} />
              <span className="d-hint">
                Optional. Set it when the frames are served through a CDN or a
                gateway rather than straight from the bucket host.
              </span>
            </label>
          </div>

          <div className="card-title" style={{ marginTop: 'var(--s5)' }}>
            S3 credentials
          </div>
          <div className="d-hint" style={{ marginBottom: 'var(--s3)' }}>
            {cfg.aws_uses_environment
              ? 'Currently signing with this appliance\u2019s own AWS credentials '
                + '(the ones Data Collection uses). Fill these in only if the '
                + 'destination bucket needs a different key.'
              : 'Signing with the key below rather than this appliance\u2019s own '
                + 'credentials. Clear the key ID to go back to those.'}
          </div>
          <div className="form-grid">
            <label>
              AWS access key ID
              <input type="password" disabled={busy} value={awsKeyId ?? ''}
                     placeholder={cfg.aws_access_key_id_set
                       ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (stored)'
                       : "Blank uses this appliance's credentials"}
                     onChange={e => setAwsKeyId(e.target.value)} />
              <span className="d-hint">
                {cfg.aws_access_key_id_set
                  ? 'Leave blank to keep the stored key ID.'
                  : 'Set this and the secret together.'}
              </span>
            </label>
            <label>
              AWS secret access key
              <input type="password" disabled={busy} value={awsSecret ?? ''}
                     placeholder={cfg.aws_secret_access_key_set
                       ? '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022 (stored)' : ''}
                     onChange={e => setAwsSecret(e.target.value)} />
              <span className="d-hint">
                {cfg.aws_secret_access_key_set
                  ? 'Leave blank to keep the stored secret.'
                  : 'Required when a key ID is set.'}
              </span>
            </label>
            <label>
              AWS region
              <input value={cfg.aws_region || ''} disabled={busy}
                     placeholder="ap-south-1"
                     onChange={e => set({ aws_region: e.target.value || null })} />
              <span className="d-hint">
                The bucket's region. It is part of the URL a reviewer's browser
                loads, so a wrong one gives images that resolve only via a
                redirect, or not at all.
              </span>
            </label>
          </div>

          {cfg.delivery_method === 'kafka' && <>
          <div className="card-title" style={{ marginTop: 'var(--s5)' }}>Kafka authentication</div>
          <div className="form-grid">
            <label>
              SASL mechanism
              <select value={cfg.sasl_mechanism || ''} disabled={busy}
                      onChange={e => set({ sasl_mechanism: e.target.value || null })}>
                <option value="">None</option>
                {MECHANISMS.map(m => <option key={m} value={m}>{m}</option>)}
              </select>
            </label>
            <label>
              Username
              <input value={cfg.sasl_username || ''} disabled={busy || !cfg.sasl_mechanism}
                     onChange={e => set({ sasl_username: e.target.value || null })} />
            </label>
            <label>
              Password
              <input type="password" disabled={busy || !cfg.sasl_mechanism}
                     value={password ?? ''}
                     placeholder={cfg.sasl_password_set ? '•••••••• (stored)' : ''}
                     onChange={e => setPassword(e.target.value)} />
              <span className="d-hint">
                {cfg.sasl_password_set
                  ? 'Leave blank to keep the stored password.'
                  : 'No password stored.'}
              </span>
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input type="checkbox" checked={cfg.use_tls} disabled={busy}
                     onChange={e => set({ use_tls: e.target.checked })} />
              Use TLS
            </label>
          </div>
          </>}
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div className="card-title">Destination class names</div>
        </div>
        <div className="panel-body flush">
          <div className="d-hint" style={{ padding: 'var(--s4)' }}>
            What each of your label classes is called in the receiving system.
            Leave one blank and its own key is sent instead — visible and odd in
            their queue, which is better than the box not arriving.
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 52 }}>Colour</th>
                  <th>Label class</th>
                  <th>Sent as</th>
                </tr>
              </thead>
              <tbody>
                {active.length === 0 && (
                  <tr><td colSpan={3}><div className="empty">
                    No active label classes. Add them under Label classes first.
                  </div></td></tr>
                )}
                {active.map(c => (
                  <tr key={c.key} data-class-key={c.key}>
                    <td><span style={{ display: 'inline-block', width: 16, height: 16,
                                       borderRadius: 3, background: c.color }} /></td>
                    <td>{c.label} <span className="d-hint">({c.key})</span></td>
                    <td>
                      <input value={cfg.class_map[c.key] || ''} disabled={busy}
                             placeholder={c.key}
                             onChange={e => {
                               const next = { ...cfg.class_map };
                               if (e.target.value.trim()) next[c.key] = e.target.value;
                               else delete next[c.key];
                               set({ class_map: next });
                             }} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div className="card-title">Delivery queue</div>
          <button className="btn-sm" disabled={busy} onClick={() => void reload()}>
            Refresh
          </button>
        </div>
        <div className="panel-body flush">
          {outbox && (
            <div className="d-hint" style={{ padding: 'var(--s4)' }}>
              {outbox.counts.pending} waiting · {outbox.counts.sent} published ·{' '}
              <span style={outbox.counts.failed ? { color: 'var(--danger)' } : undefined}>
                {outbox.counts.failed} failed
              </span>
            </div>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Annotation</th>
                  <th style={{ width: 100 }}>State</th>
                  <th style={{ width: 80 }}>Tries</th>
                  <th>Last error</th>
                  <th style={{ width: 90 }} />
                </tr>
              </thead>
              <tbody>
                {outbox?.entries.length === 0 && (
                  <tr><td colSpan={5}><div className="empty">
                    Nothing has been queued yet.
                  </div></td></tr>
                )}
                {outbox?.entries.map(e => (
                  <tr key={e.annotation_id} data-annotation-id={e.annotation_id}>
                    <td><code>{e.annotation_id.slice(0, 12)}</code></td>
                    <td>{e.state}</td>
                    <td>{e.attempts}</td>
                    <td className="d-hint">{e.last_error || '—'}</td>
                    <td>
                      {e.state === 'failed' && (
                        <button className="btn-sm" disabled={busy}
                                onClick={() => act(() => retryQaEntry(e.annotation_id),
                                                   'Queued again')}>
                          Retry
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}
