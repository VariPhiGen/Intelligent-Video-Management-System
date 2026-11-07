/**
 * RuleBuilder — wire a VMS event (left) to a peripheral action (right).
 *
 * Rules PERSIST now. They are rows in `alert_rules`, the engine evaluates them
 * against every detection the ingest accepts, and a match raises an alert that
 * shows up on the Trigger log tab. The dropdowns were already real — event
 * types from the analytics catalogue, targets from the camera registry, devices
 * from the peripheral inventory — and the only remaining fiction, a rule that
 * lived in browser state and never fired, is gone.
 *
 * A CAMERA IS PICKED FIRST, AND THE EVENT LIST IS ITS OWN. A rule's `activity`
 * has to equal the key an ingested event actually carries
 * (`ai_events.configured_activities`), which is per-camera — a camera not
 * configured for "Fire & Smoke" will never post that activity, so offering it
 * here would let an operator save a rule that can never fire. The event
 * dropdown is therefore built from the SELECTED camera's own
 * `analytics_config.activities`, not the full admin-managed catalogue, and is
 * disabled until a camera is chosen. There is also no zone target: a rule
 * watches one camera (the API still accepts `scope_kind: "zone"`; this form
 * just never offers it).
 *
 * A NEW RULE MOVES NOTHING UNTIL ARMED. It is `log_only`: it fires, it
 * matches, it raises its alert, and it records what it WOULD have done.
 * Arming is a separate, audited act behind `rule_manage`; an armed rule
 * switches the device through its binding (e.g. the device gateway). The Mode
 * column says which on every row — which is why it is not labelled "Enabled":
 * a log-only rule is very much enabled, it simply touches nothing.
 *
 * "UNTIL THE PERSON LEAVES THE ZONE" is a hold, not a timer, driven by the
 * live zone count (bridge/pending.py: the occupancy loop): ON the moment a
 * person is in the rule's zone, OFF the moment it reads empty. Ten minutes is
 * its safety cap. The confirmed event that follows only writes the alert.
 *
 * DURATION AND ITS REVERT TRAVEL TOGETHER. The API refuses `for_seconds`
 * without `then`, because a barrier told to open for eight seconds with no
 * follow-up command is a barrier that stays open. So an action with no known
 * counterpart cannot be given a duration here at all, rather than being offered
 * one that would be rejected on save.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { useToast } from '@/components/Toast';
import { useCameras } from '@/lib/cameras';
import {
  armRule, canArm, createRule, deleteRule, listRules, modeLabel, patchRule,
  type AlertRule, type RuleAction,
} from '@/lib/rules';
import { useAuth } from '@/lib/auth';
import {
  actionsForDevice, labelForCommand, needsMedia, usePeripherals, type Device,
} from './peripheralsData';
import { Switch } from './Switch';

/** How long an action holds, and what is sent when it expires. `revert: null`
 *  means the action has no automatic counterpart, so it cannot be timed. */
const UNTIL_EMPTY = 'Until the person leaves the zone';
const HOLDS: { label: string; seconds: number | null; until?: boolean }[] = [
  { label: 'Until an operator changes it', seconds: null },
  { label: UNTIL_EMPTY, seconds: null, until: true },
  { label: '5 seconds', seconds: 5 },
  { label: '8 seconds', seconds: 8 },
  { label: '30 seconds', seconds: 30 },
  { label: '2 minutes', seconds: 120 },
  { label: '10 minutes', seconds: 600 },
];

/** The command that undoes another, or null when there isn't one. Deriving it
 *  rather than letting an operator type it is what stops a rule storing
 *  "unlock … then flash". Keyed by the canonical command `actionsFor` sends
 *  (see its docstring), not the display label — so "Turn on" and "Sound
 *  siren" share one `on`/`off` pair rather than needing their own case each. */
function revertOf(command: string): string | null {
  switch (command) {
    case 'on': return 'off';
    case 'off': return 'on';
    case 'flash': return 'off';
    case 'unlock': return 'lock';
    case 'lock': return 'unlock';
    case 'open': return 'close';
    case 'close': return 'open';
    default: return null;          // an announcement has no scripted counterpart
  }
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="pk-field">
      <span className="pk-field-label">
        {label}{hint && <span className="pk-field-hint"> · {hint}</span>}
      </span>
      {children}
    </label>
  );
}

export function RuleBuilder({ data }: { data: ReturnType<typeof usePeripherals> }) {
  const toast = useToast();
  const { devices, activityTypes } = data;
  const { cameras } = useCameras();
  const { me } = useAuth();
  const caps = me?.permissions;

  const [rules, setRules] = useState<AlertRule[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setRules(await listRules());
      setLoadError(null);
    } catch (e: any) {
      setLoadError(e.message || 'Could not load rules');
    }
  }, []);

  useEffect(() => { reload(); }, [reload]);

  // Targets: cameras only. A rule watches one camera, never a whole zone — see
  // the module header on why zone scoping was dropped from this builder (the
  // backend still accepts it; this form just never offers it).
  const targets = useMemo(
    () => cameras.map(c => ({ value: c.slug, label: c.name })),
    [cameras],
  );

  const [name, setName] = useState('');
  const [eventType, setEventType] = useState('');
  const [target, setTarget] = useState('');
  const [confidence, setConfidence] = useState(0.75);
  const [deviceId, setDeviceId] = useState('');
  const [action, setAction] = useState('');
  const [holdLabel, setHoldLabel] = useState(HOLDS[0].label);
  const [media, setMedia] = useState('');

  const camera = cameras.find(c => c.slug === target) || null;
  // Event types on offer are exactly what THIS camera is configured to
  // detect (Config → Zones & Analytics), keyed the way an event's `activity`
  // field actually arrives — not the full admin catalogue, which would let an
  // operator build a rule for an activity the camera never runs and that can
  // therefore never fire.
  const cameraActivityKeys = useMemo(() => {
    const acts = camera?.analytics_config?.activities ?? [];
    const seen = new Set<string>();
    const out: string[] = [];
    for (const a of acts) {
      if (a.type && !seen.has(a.type)) { seen.add(a.type); out.push(a.type); }
    }
    return out;
  }, [camera]);
  const eventTypes = useMemo(
    () => cameraActivityKeys.map(key => ({
      key, label: activityTypes.find(t => t.key === key)?.label ?? key,
    })),
    [cameraActivityKeys, activityTypes],
  );
  // Keep the event type valid for whichever camera is selected — switching
  // cameras must not leave an activity armed that this one never detects.
  const effectiveEventType = eventTypes.some(t => t.key === eventType) ? eventType : '';

  const device: Device | null = devices.find(d => d.id === deviceId) || null;
  // By HA domain for a Home Assistant device, by category otherwise — see
  // actionsForDevice.
  const actions = actionsForDevice(device);
  // `action` state holds the CANONICAL command (actions[].command), not the
  // label — that command is what gets saved and sent to the bridge.
  // Keep the action valid for whichever device is selected — switching from a
  // maglock to a floodlight must not leave "unlock" armed.
  const effectiveAction = actions.some(a => a.command === action) ? action : (actions[0]?.command ?? '');
  const revert = effectiveAction ? revertOf(effectiveAction) : null;
  // No counterpart, no timing. See the module header.
  const holds = revert ? HOLDS : HOLDS.slice(0, 1);
  const hold = holds.find(h => h.label === holdLabel) ?? holds[0];

  // An HA speaker's announcement has to be told what to play; the API would
  // refuse it on every fire with missing_param:media_content_id.
  const wantsMedia = needsMedia(device, effectiveAction);

  const ready = !!name.trim() && !!effectiveEventType && !!target && !!device && !!effectiveAction
    && (!wantsMedia || !!media.trim());

  async function save() {
    if (!ready || !device) return;
    const act: RuleAction = {
      type: 'peripheral', peripheral: device.id, command: effectiveAction,
    };
    if (wantsMedia) act.media_content_id = media.trim();
    if (hold.until && revert) {
      act.until = 'zone_empty';
      act.then = revert;
      act.for_seconds = 600;           // safety cap; the zone ends it first
    } else if (hold.seconds !== null && revert) {
      act.for_seconds = hold.seconds;
      act.then = revert;
    }
    setBusy(true);
    try {
      await createRule({
        name: name.trim(),
        activity: effectiveEventType,
        scope_kind: 'camera',
        scope_value: target,
        min_confidence: confidence,
        // A hold re-arms when someone walks back in; a 60 s cooldown would
        // leave a returning intruder in silence.
        ...(hold.until ? { cooldown_s: 5 } : {}),
        actions: [act],
      });
      toast('Rule saved. It is log-only: it will raise alerts and move nothing until armed.');
      setName('');
      await reload();
    } catch (e: any) {
      toast(e.message, 'err');
    }
    setBusy(false);
  }

  async function arm(rule: AlertRule) {
    setBusy(true);
    try {
      await armRule(rule.id);
      // Said plainly, because the button's name promises more than P1 delivers.
      toast(`"${rule.name}" is armed: it will now switch its device when it fires.`);
      await reload();
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }

  async function toggle(rule: AlertRule) {
    setBusy(true);
    try {
      await patchRule(rule.id, { enabled: !rule.enabled });
      await reload();
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }

  async function remove(rule: AlertRule) {
    setBusy(true);
    try {
      await deleteRule(rule.id);
      // The one thing an operator might fear about deleting a rule.
      toast('Rule deleted. Alerts it already raised are kept.');
      await reload();
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }

  function triggerText(r: AlertRule): string {
    const scope = r.scope_kind === 'any' ? 'any camera' : r.scope_value;
    const activity = activityTypes.find(t => t.key === r.activity)?.label ?? r.activity;
    return `${activity} · ${scope}`;
  }

  function actionText(r: AlertRule): string {
    if (!r.actions.length) return 'alert only';
    return r.actions.map(a => a.type === 'peripheral'
      ? `${a.peripheral} → ${labelForCommand(a.command ?? '')}${a.until === 'zone_empty'
          ? ` until the zone is empty, then ${labelForCommand(a.then ?? '')}`
          : a.for_seconds ? ` for ${a.for_seconds}s, then ${labelForCommand(a.then ?? '')}` : ''}`
      : `webhook ${a.url_ref}`).join(', ');
  }

  return (
    <div className="fade">
      <div className="page-head">
        <div className="page-head-l">
          <h1 style={{ fontSize: 20, marginBottom: 2 }}>Automation rule builder</h1>
          <p className="lede">
            Connect what the cameras see to what the building does — no code. A new rule is
            saved <b>log-only</b>: it raises alerts and records what it would have done.
            Arming it is a separate step.
          </p>
        </div>
      </div>

      <div className="pk-builder">
        <div className="panel pk-builder-side" style={{ marginBottom: 0 }}>
          <div className="panel-body">
            <div className="pk-builder-title"><span className="pk-builder-glyph">▣</span> When this happens in VMS</div>
            <Field label="Rule name" hint="must be unique">
              <input value={name} onChange={e => setName(e.target.value)}
                     placeholder="e.g. Gate on after-hours intrusion" maxLength={120} />
            </Field>
            <Field label="Camera" hint={targets.length ? 'from the camera registry' : undefined}>
              <select value={target} onChange={e => setTarget(e.target.value)}>
                <option value="">
                  {targets.length ? 'Select a camera…' : 'No cameras onboarded'}
                </option>
                {targets.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
              </select>
            </Field>
            <Field label="Event type" hint={
              !target ? undefined
                : eventTypes.length ? 'activities enabled on this camera'
                : 'this camera has no activities configured'
            }>
              <select value={effectiveEventType} onChange={e => setEventType(e.target.value)}
                disabled={!target}>
                <option value="">
                  {!target ? 'Pick a camera first'
                    : eventTypes.length ? 'Select an event…' : 'No activities configured'}
                </option>
                {eventTypes.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
            </Field>
            <Field label="Minimum AI confidence">
              <div className="pk-slider-row">
                <input type="range" min={0} max={1} step={0.01} value={confidence}
                       onChange={e => setConfidence(Number(e.target.value))} />
                <span className="pk-slider-val">{confidence.toFixed(2)}</span>
              </div>
            </Field>
          </div>
        </div>

        <div className="pk-builder-arrow" aria-hidden>→</div>

        <div className="panel pk-builder-side" style={{ marginBottom: 0 }}>
          <div className="panel-body">
            <div className="pk-builder-title"><span className="pk-builder-glyph accent">⚡</span> Do this on site</div>
            <Field label="Peripheral" hint={devices.length ? 'from your inventory' : undefined}>
              <select value={deviceId} onChange={e => setDeviceId(e.target.value)}>
                <option value="">
                  {devices.length ? 'Select a device…' : 'No peripherals added yet'}
                </option>
                {devices.map(d => (
                  <option key={d.id} value={d.id}>
                    {d.name}{d.location && d.location !== '—' ? ` — ${d.location}` : ''}
                  </option>
                ))}
              </select>
              {device && (
                <span className="pk-field-hint" style={{ display: 'block', marginTop: 4 }}>
                  {/* The BINDING, which is what a fire actually uses — not the
                      inventory's free-text note of where the device lives. */}
                  {device.binding
                    ? <>via {device.binding.driver === 'homeassistant' ? 'Home Assistant'
                              : device.binding.driver}{' '}
                        <code>{device.binding.address}</code></>
                    : 'not bound yet — set its address on the Status panel'}
                </span>
              )}
            </Field>
            <Field label="Action" hint={device
                ? (device.binding?.driver === 'homeassistant'
                    ? `Home Assistant ${device.binding.address.split('.', 1)[0]} actions`
                    : `${device.category} actions`)
                : undefined}>
              <select value={effectiveAction} onChange={e => setAction(e.target.value)}
                disabled={!device}>
                {actions.length
                  ? actions.map(a => <option key={a.command} value={a.command}>{a.label}</option>)
                  : <option value="">Pick a device first</option>}
              </select>
            </Field>
            {wantsMedia && (
              <Field label="What to play" hint="a sound or text-to-speech URL the speaker can reach">
                <input value={media} onChange={e => setMedia(e.target.value)}
                       placeholder="http://192.168.1.200:8099/sounds/evacuate.mp3" />
              </Field>
            )}
            <Field label="Hold for" hint={revert ? `then ${labelForCommand(revert)}` : 'this action has no automatic revert'}>
              <select value={hold.label} onChange={e => setHoldLabel(e.target.value)}
                      disabled={!revert}>
                {holds.map(h => <option key={h.label} value={h.label}>{h.label}</option>)}
              </select>
            </Field>
          </div>
        </div>
      </div>

      <div className="panel pk-actbar">
        <div className="pk-actbar-l">
          <div>
            <div style={{ fontWeight: 600, fontSize: 13 }}>Saved as log-only</div>
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>
              The rule will evaluate every matching detection and raise alerts. Arm it below
              when you have watched it behave.
            </div>
          </div>
        </div>
        <div className="pk-actbar-r">
          <button className="btn-primary" onClick={save} disabled={!ready || busy}
            title={ready ? undefined : 'Name the rule, then pick a camera, an event and a device'}>
            {busy ? 'Saving…' : 'Save rule'}
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div className="panel-head-l">
            <div><div className="panel-title">Rules</div></div>
          </div>
        </div>
        <div className="panel-body flush">
          {loadError && (
            <div className="cfg-note act-required" style={{ margin: 'var(--s3)' }}>
              <span>◇</span><span>{loadError}</span>
            </div>
          )}
          {rules.length ? rules.map(r => (
            <div key={r.id} className="pk-rule">
              <span className="d" style={{ background: r.enabled ? 'var(--green)' : 'var(--dim)', width: 8, height: 8, borderRadius: '50%', flexShrink: 0 }} />
              <span className="pk-rule-trigger">{r.name}</span>
              <span className="pk-rule-arrow">→</span>
              <span className="pk-rule-action" title={`${triggerText(r)} → ${actionText(r)}`}>
                {actionText(r)}
              </span>
              <span style={{ flex: 1 }} />
              {/* NOT labelled "Enabled": a log-only rule is enabled and fires.
                  What this column says is whether it can act. */}
              <span className={`badge ${r.mode === 'armed' ? 'badge-amber' : 'badge-gray'}`}
                    title={r.mode === 'armed'
                      ? 'This rule may act on a device. No bridge exists yet, so it records what it would have done.'
                      : 'Raises alerts and records what it would have done. Moves nothing.'}>
                <span className="badge-dot" />{modeLabel(r.mode)}
              </span>
              {canArm(caps, r.mode) && (
                <button className="btn-ghost btn-sm" disabled={busy} onClick={() => arm(r)}
                        title="Let this rule act on its device. Audited.">
                  Arm
                </button>
              )}
              <Switch checked={r.enabled} onChange={() => toggle(r)}
                      title={r.enabled ? 'Stop evaluating this rule' : 'Evaluate this rule'} />
              <button className="iconbtn sm" title="Delete rule — alerts it raised are kept"
                      onClick={() => remove(r)}>✕</button>
            </div>
          )) : !loadError && (
            <div className="emptystate" style={{ border: 'none' }}>
              <div className="glyph">⚡</div>
              <h4>No rules yet</h4>
              <p>
                Build one above — pick a VMS trigger and the peripheral action it should fire.
                It will be saved log-only, so you can watch what it matches before arming it.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
