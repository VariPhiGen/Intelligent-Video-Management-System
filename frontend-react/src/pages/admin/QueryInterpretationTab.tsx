/**
 * QueryInterpretationTab — which language model turns a Smart Search sentence
 * into cameras, times and a search, and which features use it.
 *
 * Laid out as the decisions an administrator makes, in order: which model
 * (and therefore where query text goes), which features are on, and the
 * limits applied to every interpreted search. Each feature says whether it
 * is ready, because the server will not run one that has not passed its test.
 *
 * Configured here only (no environment overrides), so an administrator can
 * switch provider or model at any time. Keys are write-only, as on the QA sink:
 * never populated from the server, kept when left blank, removed only on an
 * explicit Remove. Operators' searches pick a change up on their next query.
 */
import { useEffect, useState, type ReactNode } from 'react';
import { plannerSettings, savePlanner, testPlanner, testAssistant, type PlannerSettings as Settings,
  type PlannerProvider, type ReasoningEffort } from '@/lib/searchPlanner';

const PROVIDERS: { id: PlannerProvider; name: string; blurb: string }[] = [
  { id: 'qwen', name: 'Local', blurb: 'Your own model server. Query text never leaves the site.' },
  { id: 'openai', name: 'OpenAI', blurb: 'Query text is sent to OpenAI. Footage and images stay local.' },
  { id: 'anthropic', name: 'Claude', blurb: 'Query text is sent to Anthropic. Footage and images stay local.' },
];
const LIMITS = [
  ['point_minutes', 'Point-in-time tolerance', 'minutes', 1, 60, 'A question like "at 10 PM" searches this many minutes either side.'],
  ['lookback_hours', 'Default lookback', 'hours', 1, 720, 'Used when a question gives no time at all.'],
  ['max_window_days', 'Longest search window', 'days', 1, 366, 'Questions asking for a longer period are asked to narrow it.'],
  ['timeout_seconds', 'Interpretation timeout', 'seconds', 3, 60, 'How long to wait for the model before giving up.'],
] as const;
type Result = { ok: boolean; text: string } | null;

function ProviderIcon({ id }: { id: PlannerProvider }) {
  return <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {id === 'qwen' ? <><rect x="4" y="4" width="16" height="6" rx="1.5" /><rect x="4" y="14" width="16" height="6" rx="1.5" /><path d="M8 7h.01M8 17h.01" /></>
      : <path d="M7 18a4 4 0 0 1-.6-7.96A6 6 0 0 1 18 9a4.5 4.5 0 0 1-.5 9z" />}
  </svg>;
}

function Status({ on, ready, pending }: { on: boolean; ready: boolean; pending: string }) {
  if (!on) return <span className="badge badge-gray"><span className="badge-dot" />Off</span>;
  return ready ? <span className="badge badge-green"><span className="badge-dot" />Ready</span>
    : <span className="badge badge-yellow"><span className="badge-dot" />{pending}</span>;
}

function Outcome({ result }: { result: Result }) {
  return result ? <p role="status" className={`qi-result ${result.ok ? 'ei-ok' : 'ei-bad'}`}>{result.text}</p> : null;
}

export function QueryInterpretationTab() {
  const [value, setValue] = useState<Settings | null>(null);
  const [saved, setSaved] = useState<Settings | null>(null);
  const [keys, setKeys] = useState<Partial<Record<PlannerProvider, string>>>({});
  const [busy, setBusy] = useState<'' | 'test' | 'assistant' | 'save'>('');
  const [loadError, setLoadError] = useState('');
  const [connection, setConnection] = useState<Result>(null);
  const [assistant, setAssistant] = useState<Result>(null);
  const [saveResult, setSaveResult] = useState<Result>(null);
  useEffect(() => {
    let live = true;
    plannerSettings().then(s => { if (live) { setValue(s); setSaved(s); } })
      .catch(() => { if (live) setLoadError('Could not load query interpretation settings.'); });
    return () => { live = false; };
  }, []);
  if (!value || !saved) return <p role="status">{loadError || 'Loading settings…'}</p>;

  const config = value.config, provider = config.provider, profile = config.profiles[provider];
  const dirty = JSON.stringify(config) !== JSON.stringify(saved.config) || Object.keys(keys).length > 0;
  const update = (patch: Partial<typeof config>) => { setValue({ ...value, config: { ...config, ...patch } }); setSaveResult(null); };
  const updateProfile = (patch: Partial<typeof profile>) => update({ profiles: { ...config.profiles, [provider]: { ...profile, ...patch } } });
  const fail = (e: unknown, fallback: string) => ({ ok: false, text: e instanceof Error ? e.message : fallback });
  const working = busy !== '';

  async function test() {
    setBusy('test'); setConnection(null);
    try { await testPlanner(config, keys); setConnection({ ok: true, text: 'Connection and sample interpretation passed. Save to apply these settings.' }); }
    catch (e) { setConnection(fail(e, 'Connection test failed.')); }
    finally { setBusy(''); }
  }
  async function testAssist() {
    setBusy('assistant'); setAssistant({ ok: true, text: 'Testing Assistant search and follow-up interpretation…' });
    try { await testAssistant(config, keys); setAssistant({ ok: true, text: 'Assistant evaluation passed. Save to apply.' }); }
    catch (e) { setAssistant(fail(e, 'Assistant evaluation failed.')); }
    finally { setBusy(''); }
  }
  async function save() {
    setBusy('save'); setSaveResult(null);
    try { const next = await savePlanner(config, keys); setValue(next); setSaved(next); setKeys({}); setSaveResult({ ok: true, text: 'Settings saved.' }); }
    catch (e) { setSaveResult(fail(e, 'Could not save settings.')); }
    finally { setBusy(''); }
  }

  const keyField = (label: string, hint?: ReactNode) => <label className="form-group">{label}
    <span className="ei-inline">
      <input type="password" autoComplete="new-password" aria-label={label} value={keys[provider] ?? ''} disabled={working}
        onChange={e => setKeys(k => ({ ...k, [provider]: e.target.value }))}
        placeholder={value.key_configured[provider] ? 'Saved — leave blank to keep' : 'Not configured'} />
      {value.key_configured[provider] && <button type="button" className="btn-ghost btn-sm ei-danger" disabled={working}
        onClick={() => setKeys(k => ({ ...k, [provider]: '' }))}>{keys[provider] === '' ? 'Key will be removed on save' : 'Remove saved key'}</button>}
    </span>
    {hint && <span className="hint">{hint}</span>}
  </label>;

  // The server's readiness describes the SAVED settings; unsaved edits need a save first.
  const searchReady = saved.config.enabled && saved.tested && !dirty;
  const assistantReady = !!saved.config.assistant_enabled && !!saved.assistant_tested && !dirty;

  return <div className="qi">
    <div className="ei-head">
      <div>
        <h3 className="ei-title">Query interpretation</h3>
        <p className="panel-sub">A language model turns plain questions in Smart Search into cameras, times and a search.</p>
      </div>
    </div>

    <section className="panel">
      <div className="panel-head"><div><div className="panel-title">Model</div>
        <div className="panel-sub">Which model reads the questions, and so where the question text goes.</div></div></div>
      <div className="panel-body">
        <div className="qi-providers" role="radiogroup" aria-label="Provider">
          {PROVIDERS.map(p => <button type="button" key={p.id} role="radio" aria-checked={provider === p.id} disabled={working}
            className={`wz-method tile-btn${provider === p.id ? ' active' : ''}`} onClick={() => { update({ provider: p.id }); setConnection(null); }}>
            <span className="wz-mi"><ProviderIcon id={p.id} /></span><b>{p.name}</b><span>{p.blurb}</span></button>)}
        </div>
        <div className="form-grid">
          {provider === 'qwen'
            ? <label className="form-group">Server URL
                <input aria-label="Server URL" value={profile.base_url} disabled={working} onChange={e => updateProfile({ base_url: e.target.value })} placeholder="http://your-model-server:8000/v1" />
                <span className="hint">An OpenAI-compatible endpoint, such as vLLM or Ollama.</span></label>
            : keyField(provider === 'anthropic' ? 'Anthropic API key' : 'OpenAI API key')}
          <label className="form-group">Model
            <input aria-label="Model" value={profile.model} disabled={working} onChange={e => updateProfile({ model: e.target.value })}
              placeholder={provider === 'qwen' ? 'Model name served by your server' : provider === 'openai' ? 'OpenAI model ID' : 'Claude model ID'} /></label>
        </div>
        <details className="ei-advanced"><summary>Advanced</summary>
          <div className="form-grid">
            {provider === 'qwen' && keyField('Local API key (optional)', 'Only if your model server requires one.')}
            {provider !== 'qwen' && <label className="form-group">Reasoning effort
              <select aria-label="Reasoning effort" value={profile.reasoning_effort} disabled={working} onChange={e => updateProfile({ reasoning_effort: e.target.value as ReasoningEffort })}>
                <option value="low">Low (recommended)</option>
                <option value="default">Model default (for models without an effort option)</option>
              </select><span className="hint">Low is faster and cheaper, and is enough for interpreting a search.</span></label>}
          </div>
        </details>
        <div className="qi-actions">
          <button type="button" className="btn-ghost btn-sm" disabled={working} onClick={test}>{busy === 'test' ? 'Testing…' : 'Test connection'}</button>
          <span className="hint">Sends one sample question to the model. Nothing is saved.</span>
        </div>
        <Outcome result={connection} />
      </div>
    </section>

    <section className="panel">
      <div className="panel-head"><div><div className="panel-title">Features</div>
        <div className="panel-sub">What operators can do with the model in Smart Search.</div></div></div>
      <div className="panel-body flush">
        <div className="ei-setting">
          <div><b className="qi-feature">Natural-language search <Status on={config.enabled} ready={searchReady} pending={dirty ? 'Save to apply' : 'Needs a test'} /></b><span>Operators type a sentence, such as "person in a red jacket at Gate 1 last night", instead of setting each filter.</span></div>
          <input type="checkbox" role="switch" className="ei-switch" aria-label="Enable query interpretation" checked={config.enabled} disabled={working}
            onChange={e => update({ enabled: e.target.checked })} />
        </div>
        <div className="ei-setting">
          <div><b className="qi-feature">Assistant <Status on={!!config.assistant_enabled} ready={assistantReady} pending={dirty ? 'Save to apply' : 'Tested on save'} /></b><span>Conversational investigations: ask, refine with follow-ups, and count stored AI events. Saving with this on runs a longer Assistant evaluation first.</span>
            <span className="qi-actions"><button type="button" className="btn-ghost btn-sm" disabled={working} onClick={testAssist}>{busy === 'assistant' ? 'Testing…' : 'Test Assistant'}</button>
              <span className="hint">Runs the full evaluation without saving. It makes several model calls.</span></span>
            <Outcome result={assistant} /></div>
          <input type="checkbox" role="switch" className="ei-switch" aria-label="Enable Assistant" checked={!!config.assistant_enabled} disabled={working}
            onChange={e => update({ assistant_enabled: e.target.checked })} />
        </div>
      </div>
    </section>

    <section className="panel">
      <div className="panel-head"><div><div className="panel-title">Search limits</div>
        <div className="panel-sub">Applied to every interpreted question, whichever model reads it.</div></div></div>
      <div className="panel-body"><div className="qi-limits">
        {LIMITS.map(([field, label, unit, min, max, hint]) => <label key={field} className="form-group">{label}
          <span className="ei-inline"><input type="number" aria-label={label} min={min} max={max} value={config[field]} disabled={working}
            onChange={e => update({ [field]: Number(e.target.value) })} /><span className="es-unit">{unit}</span></span>
          <span className="hint">{hint}</span></label>)}
      </div></div>
    </section>

    <div className={`qi-savebar${dirty ? ' dirty' : ''}`}>
      <span role="status" className={saveResult ? (saveResult.ok ? 'ei-ok' : 'ei-bad') : 'hint'}>
        {saveResult ? saveResult.text : dirty ? 'You have unsaved changes.' : 'All changes saved.'}</span>
      {dirty && <button type="button" className="btn-ghost btn-sm" disabled={working} onClick={() => { setValue(saved); setKeys({}); setSaveResult(null); }}>Discard</button>}
      <button type="button" className="btn-primary btn-sm" disabled={working} onClick={save}>
        {busy === 'save' ? (config.assistant_enabled && !saved.assistant_tested ? 'Evaluating Assistant…' : 'Saving…') : 'Save settings'}</button>
    </div>
  </div>;
}
