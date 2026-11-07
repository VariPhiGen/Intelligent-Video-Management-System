/**
 * EventIntegrationsTab — where AI events, alerts and camera health leave this VMS.
 *
 * The page answers one question first: what is being sent where, and is it
 * working? So destinations are a status board — each row names its target
 * and says Delivering / Failing / Off with the reason — and everything that is
 * set once per site (the master switch, playback links, the pull feed, the
 * mail server) lives in Settings instead of above the list.
 *
 * Secrets are write-only, as the API is: never shown, and an untouched field
 * leaves the stored one alone.
 */
import { useEffect, useState, type ReactNode } from 'react';
import { Modal } from '@/components/Modal';
import { EventSubscriptionModal } from './EventSubscriptionModal';
import * as api from '@/lib/eventIntegrations';
import type { Destination, DestinationKind, Subscription, Delivery, Controls, Options, MailServer } from '@/lib/eventIntegrations';

const TYPES: [string, string][] = [['ai.event.created', 'AI event started'], ['ai.event.closed', 'AI event ended'], ['alert.raised', 'Alert raised'], ['camera.offline', 'Camera offline'], ['camera.online', 'Camera online']];
const TYPE_LABEL = Object.fromEntries(TYPES);
const DEFAULT_RATES: Record<DestinationKind, number> = { webhook: 6000, kafka: 6000, telegram: 20, slack: 60, email: 30, sftp: 600 };
const KINDS: { kind: DestinationKind; name: string; blurb: string }[] = [
  { kind: 'email', name: 'Email', blurb: 'Alerts to people through the site mail server.' },
  { kind: 'telegram', name: 'Telegram', blurb: 'Messages to a group or channel, with a camera frame.' },
  { kind: 'slack', name: 'Slack', blurb: 'Messages and event snapshots to a Slack channel.' },
  { kind: 'webhook', name: 'Webhook', blurb: 'JSON to your own system over HTTPS.' },
  { kind: 'kafka', name: 'Kafka', blurb: 'Every event onto a topic for downstream systems.' },
  { kind: 'sftp', name: 'SFTP export', blurb: 'Event files and snapshots to a NAS or evidence server.' },
];
const KIND_NAME = Object.fromEntries(KINDS.map(k => [k.kind, k.name])) as Record<DestinationKind, string>;
const MEDIA_KINDS: DestinationKind[] = ['telegram', 'slack', 'email', 'sftp'];
// Freshness and cooldown defaults, mirroring routing.py.
const MAX_AGE: Partial<Record<DestinationKind, number>> = { telegram: 120, slack: 120, email: 3600, sftp: 604800 };
const KIND_CONFIG: Record<DestinationKind, Record<string, any>> = {
  webhook: { url: '', auth: 'none' }, telegram: { chat_id: '' }, slack: { mode: 'bot', channel_id: '' },
  kafka: { bootstrap: '', topic: 'vms.events', security: 'plaintext', sasl_mechanism: 'PLAIN' },
  email: { to: [], cc: [], bcc: [], subject_prefix: '' },
  sftp: { host: '', port: 22, username: '', auth: 'password', root: '.', host_key_fingerprint: '', snapshot_required: false },
};
const splitList = (v: string) => v.split(/[,;\s]+/).map(x => x.trim()).filter(Boolean);
const STATES = ['pending', 'sending', 'sent', 'retrying', 'failed', 'expired', 'suppressed'];
const STATE_BADGE: Record<string, string> = { sent: 'badge-green', failed: 'badge-red', retrying: 'badge-yellow', pending: 'badge-cyan', sending: 'badge-cyan', expired: 'badge-gray', suppressed: 'badge-gray' };
const blank = (): Partial<Destination> & { secret: string } => ({ name: '', kind: 'email', config: { ...KIND_CONFIG.email }, secret: '', enabled: false, media: false, allow_insecure: false, rate_per_min: DEFAULT_RATES.email, max_age_s: null });
const emptySub = (): Omit<Subscription, 'id'> => ({ destination_id: '', name: '', types: ['alert.raised'], filters: {}, cooldown_s: null, enabled: true });
const errorText = (e: unknown) => e instanceof Error ? e.message : 'Request failed';
const at = (v: string | null) => v ? new Date(v).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : 'Never';
type Stats = { queued: number; sent_24h: number; failed_24h: number };
type Run = (work: () => Promise<unknown>) => Promise<void>;
const PAGES = ['Destinations', 'What to send', 'Delivery history', 'Settings'] as const;

/** What a destination points at, in the words an admin would use. */
function target(d: Destination): string {
  const c = d.config || {};
  switch (d.kind) {
    case 'email': { const to: string[] = c.to || []; return to.length ? to[0] + (to.length > 1 ? ` and ${to.length - 1} more` : '') : 'No recipients'; }
    case 'sftp': return `${c.host}${c.port && c.port !== 22 ? `:${c.port}` : ''} in ${c.root || '.'}`;
    case 'webhook': try { return new URL(c.url).host; } catch { return c.url || 'No URL'; }
    case 'telegram': return `Chat ${c.chat_id}${c.topic_id ? `, topic ${c.topic_id}` : ''}`;
    case 'kafka': return `Topic ${c.topic} on ${(c.bootstrap || '').split(',')[0]}`;
    case 'slack': return c.mode === 'bot' ? `Channel ${c.channel_id || 'not set'}` : 'Incoming webhook';
  }
}

/** Off / Failing / Some failures / Delivering / Not used yet — and why. */
function health(d: Destination, s?: Stats): { badge: string; text: string } {
  if (!d.enabled) return { badge: 'badge-gray', text: 'Off' };
  const failingNow = d.last_error_at && (!d.last_success_at || d.last_error_at > d.last_success_at);
  if (failingNow) return { badge: 'badge-red', text: 'Failing' };
  if (s?.failed_24h) return { badge: 'badge-yellow', text: 'Some failures' };
  if (d.last_success_at) return { badge: 'badge-green', text: 'Delivering' };
  return { badge: 'badge-gray', text: 'Not used yet' };
}

function KindIcon({ kind }: { kind: DestinationKind }) {
  const paths: Record<DestinationKind, ReactNode> = {
    email: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></>,
    telegram: <path d="M21 4 3 11l6 2 2 6 3-4 5 4z" />,
    slack: <><path d="M9 3v18M15 3v18M3 9h18M3 15h18" /></>,
    webhook: <><path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1" /><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" /></>,
    kafka: <><circle cx="6" cy="12" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="18" cy="18" r="2" /><path d="m8 11 8-4M8 13l8 4" /></>,
    sftp: <><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><path d="M12 15v-5m-2 2 2-2 2 2" /></>,
  };
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[kind]}</svg>;
}

function Switch({ label, checked, disabled, onChange }: { label: string; checked: boolean; disabled?: boolean; onChange: (v: boolean) => void }) {
  return <input type="checkbox" role="switch" className="ei-switch" aria-label={label} checked={checked} disabled={disabled} onChange={e => onChange(e.target.checked)} />;
}

export function EventIntegrationsTab() {
  const [page, setPage] = useState<typeof PAGES[number]>('Destinations');
  const [destinations, setDestinations] = useState<Destination[]>([]);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [controls, setControls] = useState<Controls | null>(null);
  const [options, setOptions] = useState<Options>({ cameras: [], activities: [], rules: [] });
  const [stats, setStats] = useState<Record<string, Stats>>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<string | null | undefined>(undefined);
  const [draft, setDraft] = useState(blank);
  const [subEditing, setSubEditing] = useState<string | null | undefined>(undefined);
  const [sub, setSub] = useState(emptySub);
  const [tests, setTests] = useState<Record<string, { ok: boolean; text: string }>>({});
  const [testCamera, setTestCamera] = useState<Record<string, string>>({});
  const [history, setHistory] = useState<Delivery[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [destFilter, setDestFilter] = useState('');
  const [stateFilter, setStateFilter] = useState('');
  const [baseUrl, setBaseUrl] = useState<string | null>(null);
  const [mail, setMail] = useState<MailServer | null>(null);

  async function refresh() {
    const [ds, ss, cs, os] = await Promise.all([api.listDestinations(), api.listSubscriptions(), api.getControls(), api.getOptions()]);
    setDestinations(ds); setSubscriptions(ss); setControls(cs); setOptions(os);
    setMail(await api.getMailServer());
    const results = await Promise.all(ds.map(async d => [d.id, await api.destinationStatus(d.id)] as const));
    setStats(Object.fromEntries(results));
  }
  async function loadHistory(more = false) {
    // Read the page before touching state: a bad response becomes an error
    // banner here, not a crash inside the state updater during render.
    const { items, next: following } = await api.listDeliveries(destFilter, stateFilter, more && next ? next : undefined);
    setHistory(old => more ? [...old, ...items] : items); setNext(following);
  }
  const run: Run = async work => {
    setBusy(true); setError('');
    try { await work(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  useEffect(() => { refresh().catch(e => setError(errorText(e))).finally(() => setLoading(false)); }, []);
  useEffect(() => { if (page === 'Delivery history') loadHistory().catch(e => setError(errorText(e))); }, [page, destFilter, stateFilter]);

  const paused = controls && (!controls.enabled || !controls.environment_enabled);
  const destName = (id: string) => destinations.find(d => d.id === id)?.name || 'Deleted destination';
  const openNew = () => { setDraft(blank()); setEditing(null); };

  return <div className="ei">
    <div className="ei-head">
      <div>
        <h3 className="ei-title">Event integrations</h3>
        <p className="panel-sub">Send AI events, alerts and camera health updates to people and other systems.</p>
      </div>
      {controls && <span className={`badge ${paused ? 'badge-yellow' : 'badge-green'}`}><span className="badge-dot" />{paused ? 'Delivery paused' : 'Delivery on'}</span>}
    </div>
    {paused && <div className="banner banner-warn ei-gap">
      <div><b>Nothing is being sent.</b>
        <span className="banner-detail">{!controls!.environment_enabled
          ? 'Delivery is paused by server configuration (EVENTS_ENABLED).'
          : 'Event delivery is switched off. Turn it back on in Settings; queued events wait until then.'}</span></div>
    </div>}
    {error && <div className="banner banner-crit ei-gap" role="alert">{error}</div>}
    <div className="tabs" role="tablist">{PAGES.map(t => <button key={t} role="tab" aria-selected={page === t} className={`tab${page === t ? ' active' : ''}`} onClick={() => setPage(t)}>{t}</button>)}</div>

    {page === 'Destinations' && <section className="panel">
      <div className="panel-head">
        <div><div className="panel-title">Destinations</div>
          <div className="panel-sub">{destinations.length ? `${destinations.filter(d => d.enabled).length} of ${destinations.length} on` : 'Where events are delivered'}</div></div>
        <div className="panel-actions"><button className="btn-primary btn-sm" onClick={openNew}>Add destination</button></div>
      </div>
      {loading ? <div className="panel-body"><div className="skel-stack"><div className="skel" /><div className="skel" /><div className="skel" /></div></div>
        : !destinations.length ? <div className="empty">
          <p>No integrations. Nothing leaves this VMS until you add and turn on a destination.</p>
          <button className="btn-ghost" onClick={openNew}>Add your first destination</button>
        </div>
        : <ul className="ei-list">{destinations.map(d => {
          const h = health(d, stats[d.id]);
          const s = stats[d.id];
          const result = tests[d.id];
          return <li key={d.id} className="ei-dest">
            <div className="ei-dest-main">
              <span className="ei-kind" title={KIND_NAME[d.kind]}><KindIcon kind={d.kind} /></span>
              <div className="ei-dest-name">
                <strong>{d.name}</strong>
                <span>{KIND_NAME[d.kind]}: {target(d)}</span>
              </div>
              <span className={`badge ${h.badge}`}><span className="badge-dot" />{h.text}</span>
              <div className="ei-dest-stats">{s
                ? <span><b>{s.sent_24h}</b> sent, <b className={s.failed_24h ? 'ei-bad' : ''}>{s.failed_24h}</b> failed{s.queued ? <>, <b>{s.queued}</b> queued</> : null} in 24 h</span>
                : <span className="skel" style={{ width: 120, marginLeft: 'auto' }} />}
                <span className="ei-last">{d.last_success_at ? `Last success ${at(d.last_success_at)}` : 'No successful delivery yet'}</span></div>
              <Switch label={`Enable ${d.name}`} checked={d.enabled} disabled={busy}
                onChange={v => run(async () => { await api.saveDestination(d.id, { enabled: v }); await refresh(); })} />
            </div>
            {d.last_error && h.text !== 'Off' && <p className="ei-dest-error">Last error, {at(d.last_error_at)}: {d.last_error}</p>}
            <div className="ei-dest-actions">
              <button className="btn-ghost btn-sm" disabled={busy} onClick={() => run(async () => {
                const r = await api.testDestination(d.id, !!testCamera[d.id], testCamera[d.id]);
                setTests(t => ({ ...t, [d.id]: r.ok
                  ? { ok: true, text: `Test sent${r.provider_ref ? `: ${r.provider_ref}` : ''}${r.media ? ` (frame ${r.media})` : ''}` }
                  : { ok: false, text: r.error || 'Test failed' } }));
              })}>Send test</button>
              {MEDIA_KINDS.includes(d.kind) && <select className="ei-select-sm" aria-label={`Test frame for ${d.name}`} value={testCamera[d.id] || ''}
                onChange={e => setTestCamera(v => ({ ...v, [d.id]: e.target.value }))}>
                <option value="">Without a camera frame</option>
                {options.cameras.map(c => <option key={c.id} value={c.id}>With a live frame from {c.name}</option>)}
              </select>}
              {result && <span role="status" className={result.ok ? 'ei-ok' : 'ei-bad'}>{result.text}</span>}
              <span className="ei-push">
                <button className="btn-ghost btn-sm" disabled={busy} onClick={() => { setDraft({ ...d, secret: '' }); setEditing(d.id); }}>Edit</button>
                <button className="btn-ghost btn-sm ei-danger" disabled={busy} onClick={() => {
                  if (window.confirm(`Delete ${d.name}, its subscriptions and delivery history?`)) run(async () => { await api.deleteDestination(d.id); await refresh(); });
                }}>Delete</button>
              </span>
            </div>
          </li>;
        })}</ul>}
      <div className="panel-foot">New destinations start off. Queued deliveries keep the settings they were created with.</div>
    </section>}

    {page === 'What to send' && <section className="panel">
      <div className="panel-head">
        <div><div className="panel-title">What to send</div>
          <div className="panel-sub">Choose which events each destination receives. Detection conditions and schedules belong in Alert Rules.</div></div>
        <div className="panel-actions"><button className="btn-primary btn-sm" disabled={!destinations.length}
          onClick={() => { setSub({ ...emptySub(), destination_id: destinations[0]?.id || '' }); setSubEditing(null); }}>Add subscription</button></div>
      </div>
      {!subscriptions.length ? <div className="empty"><p>{destinations.length
        ? 'No subscriptions. A destination receives nothing until a subscription sends events to it.'
        : 'Add a destination first, then choose what it receives.'}</p></div>
        : <div className="table-wrap"><table>
          <thead><tr><th>Name</th><th>Destination</th><th>Events</th><th>Filters</th><th>Cooldown</th><th>On</th><th /></tr></thead>
          <tbody>{subscriptions.map(s => <tr key={s.id}>
            <td><strong>{s.name}</strong></td>
            <td>{destName(s.destination_id)}</td>
            <td><div className="ei-badges">{s.types.map(t => <span key={t} className="badge badge-gray">{TYPE_LABEL[t] || t}</span>)}</div></td>
            <td className="ei-muted">{filterSummary(s, options)}</td>
            <td className="ei-muted">{s.cooldown_s == null ? 'Default' : s.cooldown_s ? `${s.cooldown_s} s` : 'None'}</td>
            <td><Switch label={`Enable subscription ${s.name}`} checked={s.enabled} disabled={busy}
              onChange={v => run(async () => { await api.saveSubscription(s.id, { enabled: v }); await refresh(); })} /></td>
            <td className="ei-row-actions">
              <button className="btn-ghost btn-sm" onClick={() => { const { id, ...rest } = s; setSub(rest); setSubEditing(id); }}>Edit</button>
              <button className="btn-ghost btn-sm ei-danger" disabled={busy} onClick={() => { if (window.confirm(`Delete subscription ${s.name}?`)) run(async () => { await api.deleteSubscription(s.id); await refresh(); }); }}>Delete</button>
            </td>
          </tr>)}</tbody>
        </table></div>}
      <div className="panel-foot">Turning a subscription off affects future events. Turn its destination off to stop deliveries already queued.</div>
    </section>}

    {page === 'Delivery history' && <section className="panel">
      <div className="panel-head">
        <div><div className="panel-title">Delivery history</div>
          <div className="panel-sub">Each event, each destination, and what happened to it.</div></div>
        <div className="panel-actions">
          <select className="ei-select-sm" aria-label="Filter destination" value={destFilter} onChange={e => setDestFilter(e.target.value)}><option value="">All destinations</option>{destinations.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select>
          <select className="ei-select-sm" aria-label="Filter state" value={stateFilter} onChange={e => setStateFilter(e.target.value)}><option value="">All states</option>{STATES.map(s => <option key={s}>{s}</option>)}</select>
          <button className="btn-ghost btn-sm" disabled={busy} onClick={() => run(() => loadHistory())}>Refresh</button>
        </div>
      </div>
      {!history.length ? <div className="empty"><p>No delivery history for these filters.</p></div>
        : <div className="table-wrap"><table>
          <thead><tr><th>Time</th><th>Event</th><th>Destination</th><th>State</th><th>Attempts</th><th>Frame</th><th>Details</th><th /></tr></thead>
          <tbody>{history.map(d => <tr key={d.id}>
            <td className="ei-nowrap">{at(d.created_at)}</td>
            <td><div>{TYPE_LABEL[d.type] || d.type}</div><div className="ei-muted">{d.camera.name}</div></td>
            <td>{d.destination_name}</td>
            <td><span className={`badge ${STATE_BADGE[d.state] || 'badge-gray'}`}>{d.state}</span></td>
            <td className="ei-num">{d.attempts}</td>
            <td className="ei-muted">{d.media || '—'}</td>
            <td className="ei-detail" title={d.last_error || d.provider_ref || ''}>{d.last_error || d.provider_ref || '—'}</td>
            <td>{['failed', 'expired'].includes(d.state) && <button className="btn-ghost btn-sm" disabled={busy} onClick={() => {
              if (window.confirm('Retry this event using its original destination settings? This permits delivery even if the event is old.')) run(async () => { await api.retryDelivery(d.id); await loadHistory(); });
            }}>Retry</button>}</td>
          </tr>)}</tbody>
        </table></div>}
      {next && <div className="panel-foot"><button className="btn-ghost btn-sm" disabled={busy} onClick={() => run(() => loadHistory(true))}>Load more</button></div>}
    </section>}

    {page === 'Settings' && controls && <>
      <section className="panel">
        <div className="panel-head"><div><div className="panel-title">Delivery</div>
          <div className="panel-sub">Site-wide switches. Destinations and subscriptions keep their own on/off as well.</div></div></div>
        <div className="panel-body flush">
          <div className="ei-setting">
            <div><b>Allow event delivery</b><span>Off pauses every destination. Events queue and are sent when this is turned back on, while still fresh.</span></div>
            <Switch label="Allow event delivery" checked={controls.enabled} disabled={busy}
              onChange={v => run(async () => setControls(await api.saveControls(v, controls.feed_enabled)))} />
          </div>
          <div className="ei-setting">
            <div><b>Pull feed for LAN systems</b><span>Lets systems with the integration role read events from this VMS instead of receiving them.</span>
              {controls.feed_enabled && <span className="ei-endpoints">
                <code>/api/events/feed</code> and the live stream <code>/api/event-stream</code>, replayable for {controls.retention_days} days.</span>}</div>
            <Switch label="Enable pull feed for LAN systems" checked={controls.feed_enabled} disabled={busy}
              onChange={v => run(async () => setControls(await api.saveControls(controls.enabled, v)))} />
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-head"><div><div className="panel-title">Playback links</div>
          <div className="panel-sub">Messages can link straight to the moment in Playback.</div></div></div>
        <form className="panel-body ei-narrow" onSubmit={e => { e.preventDefault(); run(async () => { setControls(await api.saveControls(controls.enabled, controls.feed_enabled, (baseUrl ?? controls.public_base_url).trim())); setBaseUrl(null); }); }}>
          <label className="form-group">VMS address for playback links
            <div className="ei-inline">
              <input aria-label="VMS address for playback links" type="url" maxLength={512} value={baseUrl ?? controls.public_base_url}
                placeholder={controls.environment_base_url || 'https://vms.yoursite.lan'} onChange={e => setBaseUrl(e.target.value)} />
              <button className="btn-primary btn-sm" disabled={busy || baseUrl === null}>Save address</button>
            </div>
          </label>
          <p className="d-hint">{controls.public_base_url || controls.environment_base_url
            ? 'Messages include an "Open playback" link on this address. Applies to new events. Links require login and open only where this VMS is reachable.'
            : 'Not set: messages are sent without a playback link. Use the address people open the VMS with.'}</p>
        </form>
      </section>

      {mail && <MailServerPanel mail={mail} busy={busy} run={run} onSaved={setMail} />}
    </>}

    {editing !== undefined && <DestinationModal editing={editing} draft={draft} setDraft={setDraft} mail={mail} busy={busy}
      onClose={() => setEditing(undefined)} run={run} refresh={refresh} />}

    {subEditing !== undefined && <EventSubscriptionModal initial={sub} editing={!!subEditing} destinations={destinations} options={options} busy={busy}
      onClose={() => setSubEditing(undefined)}
      onSave={draft => run(async () => { const { destination_id, ...rest } = draft; await api.saveSubscription(subEditing, subEditing ? rest : draft); setSubEditing(undefined); await refresh(); })} />}
  </div>;
}

function filterSummary(s: Subscription, options: Options): string {
  const f = s.filters || {};
  const names = (ids: string[] | undefined, list: { id: string; name: string }[], all: string, one: string) =>
    !ids?.length ? all : ids.length === 1 ? (list.find(o => o.id === ids[0])?.name || one) : `${ids.length} ${one}s`;
  const parts = [names(f.cameras, options.cameras, 'All cameras', 'camera')];
  if (f.activities?.length) parts.push(names(f.activities, options.activities, '', 'activity').replace('activitys', 'activities'));
  if (f.rules?.length) parts.push(names(f.rules, options.rules, '', 'rule'));
  if (f.min_severity && f.min_severity !== 'info') parts.push(`${f.min_severity} and above`);
  if (f.include_motion) parts.push('with motion');
  return parts.join(', ');
}

type Draft = Partial<Destination> & { secret: string };

function DestinationModal({ editing, draft, setDraft, mail, busy, onClose, run, refresh }: {
  editing: string | null; draft: Draft; setDraft: (f: (d: Draft) => Draft) => void; mail: MailServer | null;
  busy: boolean; onClose: () => void; run: Run; refresh: () => Promise<void>;
}) {
  const cfg = draft.config || {};
  const kind = draft.kind as DestinationKind;
  const config = (key: string, value: unknown) => setDraft(d => ({ ...d, config: { ...d.config, [key]: value } }));
  const changeKind = (k: DestinationKind) => setDraft(d => ({ ...blank(), name: d.name, kind: k, rate_per_min: DEFAULT_RATES[k], config: { ...KIND_CONFIG[k] } }));
  const field = (label: string, key: string, type = 'text', hint?: string, full = false) =>
    <label className={`form-group${full ? ' full' : ''}`}>{label}
      <input aria-label={label} type={type} value={cfg[key] ?? ''} onChange={e => config(key, type === 'number' ? (e.target.value ? Number(e.target.value) : null) : e.target.value)} />
      {hint && <span className="hint">{hint}</span>}</label>;
  const listField = (label: string, key: string, required = false) =>
    <label className="form-group full">{label}
      <input aria-label={label} required={required} placeholder="name@example.com, …" value={typeof cfg[key] === 'string' ? cfg[key] : (cfg[key] || []).join(', ')} onChange={e => config(key, e.target.value)} /></label>;
  const check = (label: string, checked: boolean, onChange: (v: boolean) => void, disabled = false) =>
    <label className="ei-check"><input type="checkbox" checked={checked} disabled={disabled} onChange={e => onChange(e.target.checked)} />{label}</label>;
  const secretLabel = kind === 'slack' ? (cfg.mode === 'bot' ? 'Bot User OAuth Token (xoxb-)' : 'Slack incoming webhook URL') : kind === 'telegram' ? 'Bot token' : kind === 'sftp' ? 'SFTP password' : 'Secret (token, HMAC key or SASL password)';

  const save = () => run(async () => {
    // Recipient fields hold raw text while typing; send them as lists.
    const lists = kind === 'email' ? Object.fromEntries(['to', 'cc', 'bcc'].map(k => [k, typeof cfg[k] === 'string' ? splitList(cfg[k]) : cfg[k] || []])) : {};
    const body = { name: draft.name, ...(editing ? {} : { kind }), config: { ...cfg, ...lists }, media: draft.media, allow_insecure: draft.allow_insecure, rate_per_min: draft.rate_per_min, max_age_s: draft.max_age_s, ...(draft.secret ? { secret: draft.secret } : {}) };
    await api.saveDestination(editing, body); onClose(); await refresh();
  });

  return <Modal open width={680} onClose={onClose}
    title={editing ? `Edit ${draft.name || 'destination'}` : 'Add destination'}
    footer={<div className="ei-modal-foot">
      {!editing && <span className="hint">Starts off. Turn it on from the list after a test.</span>}
      <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
      <button className="btn-primary" form="ei-dest-form" disabled={busy}>Save destination</button></div>}>
    <form id="ei-dest-form" onSubmit={e => { e.preventDefault(); save(); }}>
      {!editing && <div className="ei-kinds" role="radiogroup" aria-label="Delivery method">
        {KINDS.map(k => <button type="button" key={k.kind} role="radio" aria-checked={kind === k.kind}
          className={`wz-method tile-btn${kind === k.kind ? ' active' : ''}`} onClick={() => changeKind(k.kind)}>
          <span className="wz-mi"><KindIcon kind={k.kind} /></span><b>{k.name}</b><span>{k.blurb}</span></button>)}
      </div>}
      {editing && <p className="ei-kind-line"><span className="ei-kind"><KindIcon kind={kind} /></span>{KIND_NAME[kind]}</p>}

      <div className="form-grid">
        <label className="form-group full">Name<input aria-label="Destination name" required maxLength={120} value={draft.name || ''} placeholder="Guard desk, Ops email, Evidence NAS…" onChange={e => setDraft(d => ({ ...d, name: e.target.value }))} /></label>
      </div>

      <h4 className="ei-section">Connection</h4>
      <div className="form-grid">
        {kind === 'email' && <>
          {!mail?.configured && <div className="banner banner-warn full ei-span">Set up the mail server in Settings before turning this destination on.</div>}
          {listField('To', 'to', true)}{listField('Cc (optional)', 'cc')}{listField('Bcc (optional)', 'bcc')}
          {field('Subject prefix (optional)', 'subject_prefix', 'text', 'Shown before every subject, e.g. [West gate].', true)}
        </>}
        {kind === 'sftp' && <>
          {field('Host', 'host')}{field('Port', 'port', 'number')}{field('Username', 'username')}
          <label className="form-group">Login with<select aria-label="SFTP login" value={cfg.auth} onChange={e => config('auth', e.target.value)}><option value="password">Password</option><option value="key">Private key</option></select></label>
          {field('Remote folder', 'root', 'text', 'Each event becomes a folder: date, camera, message ID.', true)}
          <label className="form-group full">Host key fingerprint (SHA256:…)
            <div className="ei-inline">
              <input aria-label="Host key fingerprint (SHA256:…)" value={cfg.host_key_fingerprint ?? ''} onChange={e => config('host_key_fingerprint', e.target.value)} />
              <button type="button" className="btn-ghost btn-sm" disabled={busy || !cfg.host} onClick={() => run(async () => {
                const { fingerprint } = await api.fetchHostKey(cfg.host, cfg.port || 22);
                if (window.confirm(`The server presented:\n\n${fingerprint}\n\nCompare it with the fingerprint shown on the server itself (ssh-keygen -lf). Use it only if they match.`)) config('host_key_fingerprint', fingerprint);
              })}>Fetch fingerprint from server</button>
            </div>
            <span className="hint">Uploads are refused if the server's key ever differs from this.</span></label>
        </>}
        {kind === 'webhook' && <>
          {field('Webhook URL', 'url', 'url', undefined, true)}
          <label className="form-group">Authentication<select aria-label="Authentication" value={cfg.auth} onChange={e => config('auth', e.target.value)}>
            <option value="none">None</option><option value="bearer">Bearer token</option><option value="hmac">HMAC signature</option></select></label>
        </>}
        {kind === 'slack' && <>
          <label className="form-group">Connection method<select aria-label="Slack connection method" value={cfg.mode || 'webhook'} onChange={e => {
            const mode = e.target.value;
            setDraft(d => ({ ...d, secret: '', media: mode === 'bot' && !!d.media,
              config: mode === 'bot' ? { mode, channel_id: '' } : { mode } }));
          }}><option value="bot">Bot token (supports images)</option><option value="webhook">Incoming webhook (text only)</option></select></label>
          {cfg.mode === 'bot' && field('Channel ID', 'channel_id', 'text', 'Open channel details in Slack and copy its channel ID (C… or G…).')}
          {cfg.mode === 'bot' && <p className="hint full">Grant files:write and chat:write, reinstall the app, and invite the bot to this channel. The token is stored encrypted. When changing connection method, enter its matching credential.</p>}
        </>}
        {kind === 'telegram' && <>{field('Chat ID', 'chat_id')}{field('Topic ID (optional)', 'topic_id', 'number')}</>}
        {kind === 'kafka' && <>
          {field('Brokers', 'bootstrap', 'text', 'host:port, comma separated.', true)}{field('Topic', 'topic')}
          <label className="form-group">Security<select value={cfg.security} onChange={e => config('security', e.target.value)}>
            <option value="plaintext">Plaintext</option><option value="ssl">TLS</option><option value="sasl_ssl">SASL over TLS</option></select></label>
          {cfg.security === 'sasl_ssl' && <>{field('SASL username', 'username')}
            <label className="form-group">SASL mechanism<select value={cfg.sasl_mechanism} onChange={e => config('sasl_mechanism', e.target.value)}>{['PLAIN', 'SCRAM-SHA-256', 'SCRAM-SHA-512'].map(k => <option key={k}>{k}</option>)}</select></label></>}
        </>}
        {kind === 'sftp' && cfg.auth === 'key'
          ? <label className="form-group full">Private key (unencrypted OpenSSH or PEM)<textarea aria-label="Destination secret" rows={4} value={draft.secret} placeholder={draft.has_secret ? 'Saved — leave blank to keep' : '-----BEGIN OPENSSH PRIVATE KEY-----'} onChange={e => setDraft(d => ({ ...d, secret: e.target.value }))} /></label>
          : kind !== 'email' && <label className="form-group full">{secretLabel}<input aria-label="Destination secret" type="password" autoComplete="new-password" value={draft.secret} placeholder={draft.has_secret ? 'Saved — leave blank to keep' : ''} onChange={e => setDraft(d => ({ ...d, secret: e.target.value }))} /></label>}
        {editing && draft.has_secret && <div className="full"><button type="button" className="btn-ghost btn-sm ei-danger" disabled={busy}
          onClick={() => run(async () => { await api.saveDestination(editing, { secret: '', enabled: false }); setDraft(d => ({ ...d, has_secret: false, secret: '' })); await refresh(); })}>Remove saved secret and turn off</button></div>}
      </div>
      {kind === 'webhook' && check('Allow plain HTTP on the LAN', !!draft.allow_insecure, v => setDraft(d => ({ ...d, allow_insecure: v })))}

      {(MEDIA_KINDS.includes(kind) && (kind !== 'slack' || cfg.mode === 'bot')) && <>
        <h4 className="ei-section">Content</h4>
        {check(kind === 'telegram' ? 'Attach camera frame' : 'Attach event snapshot', !!draft.media,
          v => setDraft(d => ({ ...d, media: v, config: { ...d.config, ...(kind === 'sftp' ? { snapshot_required: v && d.config?.snapshot_required } : {}) } })))}
        {kind === 'sftp' && check('Snapshot required (mark the delivery failed without one)', !!cfg.snapshot_required, v => config('snapshot_required', v), !draft.media)}
        <p className="d-hint">{(kind === 'telegram' || kind === 'slack') ? 'Recent alerts include a live frame labelled with its capture time. Camera status updates send text.'
          : kind === 'email' ? '"Sent" means the mail server accepted the message, not that it reached an inbox. Refused recipients appear in delivery history.'
          : 'event.json, an optional snapshot.jpg, then complete.json written last so a reader knows the folder is whole.'}</p>
      </>}

      {(kind === 'webhook' || kind === 'kafka') && <details className="ei-advanced"><summary>Custom CA certificate</summary>
        <label className="form-group">Custom CA certificate (optional)<textarea aria-label="Custom CA certificate" rows={4} value={cfg.ca_pem || ''} onChange={e => config('ca_pem', e.target.value || null)} /></label>
      </details>}

      <details className="ei-advanced"><summary>Delivery limits</summary>
        <div className="form-grid">
          <label className="form-group">Maximum deliveries per minute<input type="number" min={1} max={60000} value={draft.rate_per_min} onChange={e => setDraft(d => ({ ...d, rate_per_min: Number(e.target.value) }))} /></label>
          <label className="form-group">Maximum event age (seconds)<input type="number" min={1} max={604800} placeholder={String(MAX_AGE[kind] ?? 86400)} value={draft.max_age_s ?? ''} onChange={e => setDraft(d => ({ ...d, max_age_s: e.target.value ? Number(e.target.value) : null }))} />
            <span className="hint">Older events are not sent. Blank uses the default for {KIND_NAME[kind]}.</span></label>
        </div>
      </details>
    </form>
  </Modal>;
}

/** The one site mail server every email destination sends through. */
function MailServerPanel({ mail, busy, run, onSaved }: { mail: MailServer; busy: boolean; run: Run; onSaved: (m: MailServer) => void }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<Record<string, any>>({});
  const [password, setPassword] = useState('');
  const start = () => { setForm({ host: '', port: 587, security: 'starttls', verify_tls: true, username: '', from_address: '', from_name: '', ca_pem: '', ...mail }); setPassword(''); setOpen(true); };
  const set = (k: string, v: unknown) => setForm(f => ({ ...f, [k]: v }));
  const input = (label: string, k: string, type = 'text') => <label className="form-group">{label}<input aria-label={label} type={type} value={form[k] ?? ''} onChange={e => set(k, type === 'number' ? Number(e.target.value) : e.target.value)} /></label>;
  const security = { starttls: 'STARTTLS', tls: 'TLS', none: 'None (LAN relay)' } as Record<string, string>;
  return <section className="panel">
    <div className="panel-head">
      <div><div className="panel-title">Mail server</div>
        <div className="panel-sub">{mail.configured ? `${mail.from_address} via ${mail.host}:${mail.port}` : 'Not set up. Email destinations need it.'}</div></div>
      <div className="panel-actions"><button className={mail.configured ? 'btn-ghost btn-sm' : 'btn-primary btn-sm'} disabled={busy} onClick={start}>{mail.configured ? 'Edit' : 'Set up'}</button></div>
    </div>
    {mail.configured && <div className="panel-body"><div className="kv ei-narrow">
      <div className="kv-row"><span>Sender</span><span>{mail.from_name ? `${mail.from_name} <${mail.from_address}>` : mail.from_address}</span></div>
      <div className="kv-row"><span>Server</span><span>{mail.host}:{mail.port}</span></div>
      <div className="kv-row"><span>Security</span><span>{security[mail.security || 'starttls']}{mail.security !== 'none' && mail.verify_tls === false ? ', certificate not verified' : ''}</span></div>
      <div className="kv-row"><span>Login</span><span>{mail.username ? `${mail.username}${mail.has_password ? ', password saved' : ''}` : 'None'}</span></div>
    </div></div>}
    {open && <Modal open width={620} title={mail.configured ? 'Edit mail server' : 'Set up mail server'} onClose={() => setOpen(false)}
      footer={<div className="ei-modal-foot">
        {mail.configured && <button type="button" className="btn-ghost ei-danger ei-left" disabled={busy} onClick={() => { if (window.confirm('Remove the mail server? Email destinations will fail until it is set up again.')) run(async () => { onSaved(await api.deleteMailServer()); setOpen(false); }); }}>Remove</button>}
        <button type="button" className="btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
        <button className="btn-primary" form="ei-mail-form" disabled={busy}>Save mail server</button></div>}>
      <form id="ei-mail-form" onSubmit={e => { e.preventDefault(); run(async () => {
        const { configured, has_password, ...rest } = form;
        onSaved(await api.saveMailServer({ ...rest, username: rest.username || null, from_name: rest.from_name || null, ca_pem: rest.ca_pem || null, ...(password ? { password } : {}) }));
        setOpen(false);
      }); }}>
        <div className="form-grid">
          {input('SMTP host', 'host')}{input('Port', 'port', 'number')}
          <label className="form-group">Security<select aria-label="SMTP security" value={form.security} onChange={e => { set('security', e.target.value); set('port', e.target.value === 'tls' ? 465 : e.target.value === 'starttls' ? 587 : 25); }}>
            <option value="starttls">STARTTLS (587)</option><option value="tls">TLS (465)</option><option value="none">None — LAN relay without login</option></select></label>
          {form.security !== 'none' ? <label className="ei-check ei-align">
            <input type="checkbox" checked={form.verify_tls} onChange={e => set('verify_tls', e.target.checked)} />Verify the server certificate</label> : <span />}
          {input('Sender address', 'from_address', 'email')}{input('Sender name (optional)', 'from_name')}
          {input('Login username (optional)', 'username')}
          {form.username ? <label className="form-group">Password<input aria-label="SMTP password" type="password" autoComplete="new-password" value={password} placeholder={mail.has_password ? 'Saved — leave blank to keep' : ''} onChange={e => setPassword(e.target.value)} /></label> : <span />}
        </div>
        {form.security !== 'none' && <details className="ei-advanced"><summary>Custom CA certificate</summary>
          <label className="form-group">Custom CA certificate (optional)<textarea aria-label="SMTP CA certificate" rows={4} value={form.ca_pem || ''} onChange={e => set('ca_pem', e.target.value)} /></label></details>}
        <p className="d-hint">Password login only. Providers that require OAuth (some Microsoft 365 and Gmail setups) need an app password or an SMTP relay.</p>
      </form>
    </Modal>}
  </section>;
}
