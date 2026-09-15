/**
 * RuleBuilder — wire a VMS event (left) to a peripheral action (right).
 *
 * Every dropdown now lists real things: event types come from the analytics
 * activity-type catalogue, targets from the camera registry (zones, then
 * individual cameras), devices from the peripheral inventory, and actions are
 * derived from the selected device's category. They used to be five
 * hand-written arrays — invented zones like "Zone B — Perimeter North" and
 * invented HA entity ids — which made a rule look configurable against a site
 * that didn't exist.
 *
 * What is still not real is the *engine*: saving keeps the rule in local state
 * for the session, and nothing fires. That's what the tab's "Demo data" chip
 * refers to now.
 */
import { useMemo, useState } from 'react';
import {
  actionsFor, DURATIONS, usePeripherals, type Device,
} from './peripheralsData';
import { Switch } from './Switch';
import { useToast } from '@/components/Toast';
import { useCameras, zoneOf } from '@/lib/cameras';

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
  const { rules, toggleRule, removeRule, addRule, devices, eventTypes } = data;
  const { cameras } = useCameras();

  // Targets: every zone the registry knows, then each camera by name. Both are
  // live — the zone list is the same one the Map tab draws boxes from.
  const targets = useMemo(() => {
    const zones = [...new Set(cameras.map(zoneOf))].sort();
    return [
      ...zones.map(z => ({ value: `zone:${z}`, label: `${z} (zone)` })),
      ...cameras.map(c => ({ value: `camera:${c.slug}`, label: c.name })),
    ];
  }, [cameras]);

  const [eventType, setEventType] = useState('');
  const [target, setTarget] = useState('');
  const [confidence, setConfidence] = useState(0.75);
  const [deviceId, setDeviceId] = useState('');
  const [action, setAction] = useState('');
  const [duration, setDuration] = useState(DURATIONS[0]);

  const device: Device | null = devices.find(d => d.id === deviceId) || null;
  const actions = actionsFor(device?.category ?? null);
  // Keep the action valid for whichever device is selected — switching from a
  // maglock to a floodlight must not leave "Unlock" armed.
  const effectiveAction = actions.includes(action) ? action : (actions[0] ?? '');

  const targetLabel = targets.find(t => t.value === target)?.label ?? '';
  const ready = !!eventType && !!target && !!device && !!effectiveAction;

  const save = () => {
    if (!ready || !device) return;
    addRule(`${eventType} · ${targetLabel}`, `${device.name} → ${effectiveAction}`);
    toast('Rule saved for this session — no rule engine runs yet, so nothing will fire');
  };

  return (
    <div className="fade">
      <div className="page-head">
        <div className="page-head-l">
          <h1 style={{ fontSize: 20, marginBottom: 2 }}>Automation rule builder</h1>
          <p className="lede">Connect what the cameras see to what the building does — no code. Pick a trigger on the left, an action on the right.</p>
        </div>
      </div>

      <div className="pk-builder">
        <div className="panel pk-builder-side" style={{ marginBottom: 0 }}>
          <div className="panel-body">
            <div className="pk-builder-title"><span className="pk-builder-glyph">▣</span> When this happens in VMS</div>
            <Field label="Event type" hint={eventTypes.length ? 'from the analytics catalogue' : undefined}>
              <select value={eventType} onChange={e => setEventType(e.target.value)}>
                <option value="">
                  {eventTypes.length ? 'Select an event…' : 'No activity types configured'}
                </option>
                {eventTypes.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </Field>
            <Field label="Camera / zone" hint={targets.length ? 'from the camera registry' : undefined}>
              <select value={target} onChange={e => setTarget(e.target.value)}>
                <option value="">
                  {targets.length ? 'Select a camera or zone…' : 'No cameras onboarded'}
                </option>
                {targets.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
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
            <div className="pk-builder-title"><span className="pk-builder-glyph accent">⚡</span> Do this in Home Assistant</div>
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
                  {device.externalId
                    ? <>binds to <code>{device.externalId}</code></>
                    : 'no bridge address recorded — add one on the Status panel'}
                </span>
              )}
            </Field>
            <Field label="Action" hint={device ? `${device.category} actions` : undefined}>
              <select value={effectiveAction} onChange={e => setAction(e.target.value)}
                disabled={!device}>
                {actions.length
                  ? actions.map(a => <option key={a} value={a}>{a}</option>)
                  : <option value="">Pick a device first</option>}
              </select>
            </Field>
            <Field label="Duration">
              <select value={duration} onChange={e => setDuration(e.target.value)}>
                {DURATIONS.map(d => <option key={d} value={d}>{d}</option>)}
              </select>
            </Field>
          </div>
        </div>
      </div>

      <div className="panel pk-actbar">
        <div className="pk-actbar-l">
          <Switch checked onChange={() => toast('Dry run is the only mode — no rule engine exists to run a rule for real')}
            tone="green" title="Dry run — the only mode available" />
          <div>
            <div style={{ fontWeight: 600, fontSize: 13 }}>Dry run</div>
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>
              The only mode available: no rule engine runs, so no rule fires either way
            </div>
          </div>
        </div>
        <div className="pk-actbar-r">
          <button className="btn-ghost" disabled
            title="Testing needs the rule engine and the Home-Assistant bridge — neither exists yet">
            Test now
          </button>
          <button className="btn-primary" onClick={save} disabled={!ready}
            title={ready ? undefined : 'Pick an event, a target, and a device'}>
            Save rule
          </button>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <div className="panel-head-l">
            <div><div className="panel-title">Active rules</div></div>
          </div>
        </div>
        <div className="panel-body flush">
          {rules.length ? rules.map(r => (
            <div key={r.id} className="pk-rule">
              <span className="d" style={{ background: r.enabled ? 'var(--green)' : 'var(--dim)', width: 8, height: 8, borderRadius: '50%', flexShrink: 0 }} />
              <span className="pk-rule-trigger">{r.trigger}</span>
              <span className="pk-rule-arrow">→</span>
              <span className="pk-rule-action">{r.action}</span>
              <span style={{ flex: 1 }} />
              <span className="pk-rule-age">{r.age}</span>
              <Switch checked={r.enabled} onChange={() => toggleRule(r.id)} title={r.enabled ? 'Disable rule' : 'Enable rule'} />
              <button className="iconbtn sm" title="Remove rule" onClick={() => removeRule(r.id)}>✕</button>
            </div>
          )) : (
            <div className="emptystate" style={{ border: 'none' }}>
              <div className="glyph">⚡</div>
              <h4>No rules yet</h4>
              <p>
                Build one above — pick a VMS trigger and the peripheral action it should fire.
                Rules live for this session only until the rule engine ships.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
