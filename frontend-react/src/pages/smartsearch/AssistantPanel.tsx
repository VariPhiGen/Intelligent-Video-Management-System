import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { plannerStatus } from '@/lib/searchPlanner';
import { assistantTurn, clearInvestigation, createInvestigation, restoreInvestigation, investigationProgress,
  type Conversation, type AssistantAnswer } from '@/lib/searchAssistant';
import { ResultCard } from './ResultCard';
import { EventClipModal } from './EventClipModal';
import type { SearchCamera } from '@/lib/smartsearch';

const STORAGE = 'smartsearch-assistant-investigation';
const CHIPS = [['cameras', 'Cameras'], ['activities', 'Activities'], ['appearance', 'Appearance'], ['plate', 'Plate']] as const;
const timeWindow = (scope: { time_from: string; time_to: string; timezone: string }) => {
  const at = (v: string) => new Date(v).toLocaleString(undefined, { timeZone: scope.timezone });
  return `${at(scope.time_from)} – ${at(scope.time_to)} (${scope.timezone})`;
};
export function AssistantPanel() {
  const nav = useNavigate();
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [ready, setReady] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [timezone, setTimezone] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [stage, setStage] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [clip, setClip] = useState<{ cam: SearchCamera; whenMs: number } | null>(null);
  const live = useRef(true);
  const pending = useRef<{ message: string; request_id: string; expected_version: number; choice_id?: string; remove_filter?: string; result_id?: string; result_action?: 'play' | 'similar'; timezone?: string } | null>(null);
  const [canRetry, setCanRetry] = useState(false);
  const busyRef = useRef(false);

  useEffect(() => {
    live.current = true;
    plannerStatus().then(async status => {
      if (!live.current) return;
      setReady(!!status.assistant_ready);
      if (!status.assistant_ready) return;
      const id = sessionStorage.getItem(STORAGE);
      if (id) {
        try {
          const restored = await restoreInvestigation(id);
          if (live.current) { setConversation(restored); setTimezone(restored.timezone); }
        } catch (e) {
          if (live.current) setError(e instanceof Error ? e.message : 'Could not restore this investigation.');
        }
      }
    }).catch(e => { if (live.current) setError(e instanceof Error ? e.message : 'Could not load Assistant status.'); });
    return () => { live.current = false; };
  }, []);

  async function fresh() {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try {
      const old = conversation?.id || sessionStorage.getItem(STORAGE);
      if (old) {
        // An expired investigation needs no deletion. Other failures are shown.
        try { await clearInvestigation(old); } catch { /* New state has an independent owner-bound ID. */ }
      }
      const c = await createInvestigation(Intl.DateTimeFormat().resolvedOptions().timeZone);
      sessionStorage.setItem(STORAGE, c.id);
      if (live.current) { setConversation(c); setTimezone(c.timezone); setMessage(''); setCanRetry(false); pending.current = null; }
    } catch (e) { if (live.current) setError(e instanceof Error ? e.message : 'Could not start investigation.'); }
    finally { busyRef.current = false; if (live.current) setBusy(false); }
  }

  async function send(text: string, choice_id?: string, remove_filter?: string, retry = false, result_id?: string, newTimezone?: string) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(''); setCanRetry(false); setStage('Submitting…');
    let timer: ReturnType<typeof setInterval> | undefined;
    try {
      let c = conversation;
      if (!c) {
        c = await createInvestigation(Intl.DateTimeFormat().resolvedOptions().timeZone);
        sessionStorage.setItem(STORAGE, c.id);
        if (live.current) setConversation(c);
      }
      const id = c.id;
      const body = retry && pending.current ? pending.current : {
        message: text, request_id: crypto.randomUUID(), expected_version: c.version,
        ...(choice_id ? { choice_id } : {}), ...(remove_filter ? { remove_filter } : {}), ...(result_id ? { result_id, result_action: 'similar' as const } : {}), ...(newTimezone ? { timezone: newTimezone } : {}),
      };
      pending.current = body;
      let polling = false;
      timer = setInterval(async () => {
        if (polling) return;
        polling = true;
        try { const current = await investigationProgress(id); if (live.current && current.progress) setStage(current.progress); }
        catch { /* Turn response is authoritative; transient progress errors do not end it. */ }
        finally { polling = false; }
      }, 1500);
      const next = await assistantTurn(id, body);
      if (live.current) { setConversation(next); setMessage(''); pending.current = null; }
    } catch (e) {
      if (live.current) { setError(e instanceof Error ? e.message : 'Could not complete this turn.'); setCanRetry(!!pending.current); }
    } finally {
      if (timer) clearInterval(timer);
      busyRef.current = false;
      if (live.current) { setBusy(false); setStage(''); }
    }
  }
  function playback(camera: string, at: number) { nav(`/playback?cam=${encodeURIComponent(camera)}&t=${Math.floor(at)}`); }
  function renderAnswer(answer: AssistantAnswer, latest: boolean) {
    return <div>
      <p>{answer.message}</p>
      {answer.choices?.map(choice => <button key={choice.id} className="btn-ghost btn-sm"
        disabled={busy || !latest || !!answer.selected_choice} aria-pressed={answer.selected_choice === choice.id}
        onClick={() => send(choice.label, choice.id)}>{choice.label}{answer.selected_choice === choice.id ? ' ✓' : ''}</button>)}
      {answer.playback && <button className="btn-primary" onClick={() => playback(answer.playback!.camera, answer.playback!.start)}>Open playback</button>}
      {answer.collections.map((collection, i) => <div key={i} className="panel" style={{ marginBlock: 12 }}>
        <div className="panel-body">
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {CHIPS.map(([field, label]) => {
              // Names for cameras and activities; investigations saved before
              // labels existed fall back to the stored slug or key.
              const value = (field === 'cameras' || field === 'activities') ? collection.scope_labels?.[field] ?? collection.scope[field] : collection.scope[field];
              return value && <button key={field} className="btn-ghost btn-sm" disabled={!latest || busy}
                title={`Remove ${label.toLowerCase()} filter`} onClick={() => send('', undefined, field)}>
                {label}: {Array.isArray(value) ? value.join(', ') : value} ×</button>;
            })}
            <button className="btn-ghost btn-sm" disabled={!latest || busy} title="Remove time filter" onClick={() => send('', undefined, 'time')}>
              Time: {timeWindow(collection.scope)} ×</button>
          </div>
          {collection.interpretations?.map(text => <p key={text} className="hint">{text}</p>)}
          {collection.warnings?.map(text => <p key={text} role="status">{text}</p>)}
          {collection.error && <p role="alert">{collection.error}</p>}
          {collection.count !== undefined && <p><b>{collection.count}</b> retained AI-event records{collection.count_as_of && <span className="hint"> · counted {new Date(collection.count_as_of).toLocaleString()}</span>}</p>}
          {!!collection.groups?.length && <table><thead><tr><th>Group</th><th>Event records</th></tr></thead><tbody>
            {collection.groups.map(g => <tr key={g.label}><td>{g.label}</td><td>{g.count}</td></tr>)}
          </tbody></table>}
          {collection.setup && <details><summary>Camera setup and availability</summary>
            {collection.setup.map(c => <p key={c.camera.id}><b>{c.camera.name || c.camera.slug}</b>: {c.activities.join(', ') || 'No configured activities'}
              {' · '}{c.recording_enabled ? 'Recording enabled' : 'Recording disabled'} · Retention {c.retention_minutes} minutes
              {!c.recorder_available && ' · Recorder unavailable'}
              {c.recorded_seconds != null && ` · ${Math.round(c.recorded_seconds)} recorded seconds in this window`}
              {c.search_indexing === false && ' · Visual indexing disabled'}</p>)}
          </details>}
          <div className="ss-grid">
            {collection.items.map(item => {
              const when = item.when_ms ?? new Date(item.started_at || '').getTime();
              const visual = collection.tool === 'search_people' || collection.tool === 'search_vehicles';
              return <div key={item.id}><ResultCard title={item.activity?.label || item.plate || (collection.tool === 'search_people' ? 'Person' : 'Vehicle')}
                when={when} camera={item.camera} score={item.score} sensor={item.camera.slug} facts={[]}
                onOpen={() => playback(item.camera.slug, when / 1000)}
                onShowEvent={() => setClip({ cam: item.camera, whenMs: when })}
                shot={visual ? { domain: collection.tool === 'search_people' ? 'person' : 'vehicles', id: item.id, bbox: item.bbox, hasFrame: item.has_frame } : undefined} />
                {latest && collection.tool === 'search_people' && <button className="btn-ghost btn-sm" disabled={busy}
                  onClick={() => send('Find visually similar people', undefined, undefined, false, item.id)}>More like this</button>}
              </div>;
            })}
          </div>
          {collection.truncated && <p className="hint">Results are limited. Narrow the scope to see more detail.</p>}
          {latest && !collection.items.length && !collection.count && collection.scope.cameras?.length && <button className="btn-ghost" disabled={busy} onClick={() => send('', undefined, 'cameras')}>Search all cameras instead</button>}
          {latest && collection.next_cursor && <button className="btn-ghost" disabled={busy} onClick={() => send('Show more')}>Show more</button>}
        </div>
      </div>)}
    </div>;
  }
  return <section className="panel" aria-label="Search Assistant"><div className="panel-body">
    <h2>Assistant</h2>
    {error && <p role="alert">{error}</p>}
    {ready === null && !error && <p role="status">Loading Assistant…</p>}
    {ready === false && <p>Enable and test the Assistant in Administration → Query interpretation.</p>}
    {ready && <>
      <button className="btn-ghost" disabled={busy} onClick={fresh}>New investigation</button>
      <label style={{ marginLeft: 12 }}>Timezone <input aria-label="Investigation timezone" value={timezone} disabled={busy} onChange={e => setTimezone(e.target.value)} /></label>
      <button className="btn-ghost btn-sm" disabled={busy || !timezone.trim()} onClick={() => send('', undefined, undefined, false, undefined, timezone)}>Apply timezone</button>
      <p className="hint">Search recordings and stored events, then refine the results. Investigations expire after 30 minutes of inactivity.</p>
      {!conversation?.exchanges.length && (conversation?.suggestions || ['What can you do?']).map(q =>
        <button key={q} className="btn-ghost btn-sm" disabled={busy} onClick={() => send(q)}>{q}</button>)}
      {conversation?.exchanges.map((exchange, index) => <article key={exchange.request_id} style={{ marginBlock: 18 }}>
        <p><b>{exchange.message || 'Filter updated'}</b></p>
        {renderAnswer(exchange.answer, index === conversation.exchanges.length - 1)}
      </article>)}
      <form onSubmit={e => { e.preventDefault(); if (message.trim()) void send(message); }} style={{ display: 'flex', gap: 8 }}>
        <input aria-label="Ask Assistant" value={message} maxLength={1000} disabled={busy} style={{ flex: 1 }}
          placeholder="Ask about a camera, appearance, plate or AI event…" onChange={e => setMessage(e.target.value)} />
        <button className="btn-primary" disabled={busy || !message.trim()}>Send</button>
      </form>
      {busy && <p role="status">{stage || 'Working…'}</p>}
      {canRetry && <button className="btn-ghost" disabled={busy} onClick={() => send('', undefined, undefined, true)}>Retry this turn</button>}
    </>}
    {clip && <EventClipModal cam={clip.cam} whenMs={clip.whenMs} onClose={() => setClip(null)} />}
  </div></section>;
}
