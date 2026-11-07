/**
 * TriggerLog — what the rules actually fired, and what an operator made of it.
 *
 * This tab used to render six hardcoded rows: invented timestamps, invented
 * events, and a named operator ("R. Kulkarni") who unlocked a gate that never
 * existed, under the heading "logged for audit". A fabricated audit trail is
 * the most dangerous kind of demo data — indistinguishable from evidence, and
 * it sat beside a CSV export button that would hand it to somebody as a file.
 * It was replaced by an honest empty state, and now by real rows.
 *
 * WHAT `notified` MEANS AND WHY IT IS SHOWN. Every alert records what each of
 * its actions actually did, as a stable token the bridge wrote (`called`,
 * `held`, `log_only`, `call_rejected:modbus-tcp/timeout`, ...). `outcomeOf`
 * turns each into words and a tone, by DEVICE NAME — the difference between a
 * log that reads "the siren switched on" and one that reads "this rule would
 * have switched it on", and only one of them is ever true for a given row.
 *
 * Resolving an alert is not paperwork. `resolution` is the column that makes a
 * per-rule false-positive rate computable, and it cannot be reconstructed
 * afterwards, which is why the verdicts are a fixed set rather than a text box.
 */
import { useCallback, useEffect, useState } from 'react';

import { useToast } from '@/components/Toast';
import { apiFetch } from '@/lib/api';
import { useCameras } from '@/lib/cameras';
import {
  ackAlert, holdAlert, listAlerts, resolveAlert,
  type Alert, type Resolution,
} from '@/lib/rules';
import { useAuth } from '@/lib/auth';

const VERDICTS: { value: Resolution; label: string }[] = [
  { value: 'true_positive', label: 'Real — acted on' },
  { value: 'false_positive', label: 'False alarm' },
  { value: 'duplicate', label: 'Duplicate' },
  { value: 'no_action', label: 'Real — no action needed' },
];

const STATES = [
  ['', 'All'], ['new', 'New'], ['acknowledged', 'Acknowledged'], ['resolved', 'Resolved'],
] as const;

/** How often the log looks for new alerts. */
export const LOG_POLL_MS = 5_000;

export type OutcomeTone = 'ok' | 'muted' | 'warn' | 'bad';
export interface Outcome { device: string; text: string; tone: OutcomeTone; raw: string }

/** One device's line: what the bridge recorded, in words. Unknown tokens are
 *  shown as they are rather than hidden — never invent a happier story. */
export function outcomeOf(token: string): { text: string; tone: OutcomeTone } {
  const t = token || '';
  if (t === 'called' || t === 'published' || t === 'resolved')
    return { text: 'Switched', tone: 'ok' };
  if (t === 'held') return { text: 'Already on — holding until the zone is empty', tone: 'ok' };
  if (t === 'zone_already_empty')
    return { text: 'Person had already left — not switched', tone: 'muted' };
  if (t === 'log_only') return { text: 'Log only — rule not armed, nothing sent', tone: 'muted' };
  if (t === 'disarmed') return { text: 'Automation disarmed — nothing sent', tone: 'muted' };
  if (t === 'unconfirmed' || t === 'stale' || t === 'stale_skipped')
    return { text: 'Not sent — rule state could not be confirmed', tone: 'warn' };
  if (t === 'no_binding' || t === 'no_bridge')
    return { text: 'Not connected — the device has no binding', tone: 'warn' };
  if (t === 'binding_unverified') return { text: 'Not sent — address not verified yet', tone: 'warn' };
  if (t === 'peripheral_disabled') return { text: 'Not sent — device is disabled', tone: 'warn' };
  if (t === 'unknown_peripheral') return { text: 'Not sent — device no longer exists', tone: 'warn' };
  if (t.startsWith('call_rejected:')) return { text: `Failed: ${t.slice('call_rejected:'.length)}`, tone: 'bad' };
  if (t === 'call_failed') return { text: 'Failed: device gateway unreachable', tone: 'bad' };
  if (t === 'timeout') return { text: 'Failed: no answer in time', tone: 'bad' };
  return { text: t.replace(/_/g, ' '), tone: 'bad' };
}

/** Every device line of an alert, by name. `_actuation` says why nothing was
 *  attempted and is folded into each line rather than shown on its own. */
export function outcomesOf(notified: Record<string, string>, names: Record<string, string>): Outcome[] {
  return Object.entries(notified || {})
    .filter(([k]) => !k.startsWith('_'))
    .map(([slug, raw]) => ({ device: names[slug] ?? slug, raw, ...outcomeOf(raw) }));
}

function clock(iso: string): { time: string; day: string; ago: string } {
  const d = new Date(iso);
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  const ago = s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago`
    : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
  return {
    time: d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }),
    day: d.toLocaleDateString([], { day: 'numeric', month: 'short' }),
    ago,
  };
}

const STATE_BADGE: Record<string, string> = {
  new: 'badge-blue', acknowledged: 'badge-yellow', resolved: 'badge-green',
};

export function TriggerLog() {
  const toast = useToast();
  const { cameras } = useCameras();
  const { me } = useAuth();
  const canAck = !!me?.permissions?.alerts_ack;

  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [names, setNames] = useState<Record<string, string>>({});
  const [state, setState] = useState('');
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await listAlerts({ state: state || undefined, limit: 200 });
      setAlerts(r.alerts);
      setErr(null);
    } catch (e: any) {
      // Keep what is on screen; a failed refresh is not an empty log.
      setErr(e.message || 'Could not load alerts');
    }
  }, [state]);

  useEffect(() => {
    void load();
    const t = setInterval(() => {
      if (document.visibilityState !== 'hidden') void load();
    }, LOG_POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  // Device names, so a row says "Relay 1 · CH8 (siren)" not its slug.
  useEffect(() => {
    apiFetch<{ id: string; name: string }[]>('/peripherals')
      .then(rows => setNames(Object.fromEntries(rows.map(p => [p.id, p.name]))))
      .catch(() => { /* slugs are still true, just less friendly */ });
  }, []);

  const cameraName = (id: string) =>
    cameras.find(c => c.id === id)?.name ?? id.slice(0, 8);

  async function act(fn: () => Promise<unknown>, done: string) {
    setBusy(true);
    try {
      await fn();
      toast(done);
      await load();
    } catch (e: any) { toast(e.message, 'err'); }
    setBusy(false);
  }

  return (
    <div className="fade">
      <div className="pk-head">
        <div className="pk-head-l" style={{ color: 'var(--muted)', fontSize: 13 }}>
          Every alert an automation rule raised, what it did to each device, and what was made of it
        </div>
        <div className="pk-head-r">
          <select value={state} onChange={e => setState(e.target.value)} aria-label="Filter by state">
            {STATES.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
          </select>
        </div>
      </div>

      <div className="panel">
        <div className="panel-body flush">
          {err && (
            <div className="cfg-note act-required" style={{ margin: 'var(--s3)' }}>
              <span>◇</span><span>{err}</span>
            </div>
          )}

          {!err && !alerts.length && (
            <div className="emptystate" style={{ border: 'none' }}>
              <div className="glyph">⎋</div>
              <h4>No alerts {state ? `in this state` : 'yet'}</h4>
              <p>
                An alert appears here when a rule matches a detection. Build one on the
                Rule builder tab — a log-only rule raises alerts just as an armed one does,
                which is the point of watching it before arming it.
              </p>
              <p style={{ fontSize: 11.5, color: 'var(--dim)', marginTop: 8 }}>
                Operator actions on the VMS itself — camera edits, exports, searches —
                are logged under Administration → Audit log.
              </p>
            </div>
          )}

          {alerts.length > 0 && (
            <div className="tl-row tl-head" aria-hidden="true">
              <span>Time</span><span>Rule</span><span>What happened</span><span>Status</span>
            </div>
          )}

          {alerts.map(a => {
            const t = clock(a.triggered_at);
            const outcomes = outcomesOf(a.notified, names);
            return (
              <div key={a.id} className={`tl-row tl-sev-${a.severity}`}>
                <div className="tl-time" title={new Date(a.triggered_at).toLocaleString()}>
                  <span className="tl-clock">{t.time}</span>
                  <span className="tl-day">{t.day} · {t.ago}</span>
                </div>

                <div className="tl-rule">
                  <span className="tl-sev-dot" title={`Severity: ${a.severity}`} />
                  <div style={{ minWidth: 0 }}>
                    <div className="tl-rule-name">{a.rule_name || 'a deleted rule'}</div>
                    <div className="tl-sub">{cameraName(a.camera_id)} · {a.severity}</div>
                  </div>
                </div>

                <div className="tl-outcomes">
                  {outcomes.length ? outcomes.map(o => (
                    <div key={o.device} className={`tl-out tl-out-${o.tone}`} title={o.raw}>
                      <span className="tl-out-dev">{o.device}</span>
                      <span className="tl-out-text">{o.text}</span>
                    </div>
                  )) : <span className="tl-sub">Alert only — the rule has no device action</span>}
                  {a.resolution && (
                    <div className="tl-sub">
                      {VERDICTS.find(v => v.value === a.resolution)?.label ?? a.resolution}
                      {a.resolved_by && <> · {a.resolved_by}</>}
                      {a.resolution_note && <> — {a.resolution_note}</>}
                    </div>
                  )}
                </div>

                <div className="tl-status">
                  <div className="tl-badges">
                    <span className={`badge ${STATE_BADGE[a.state] ?? 'badge-gray'}`}>
                      <span className="badge-dot" />{a.state}
                    </span>
                    {a.legal_hold && (
                      <span className="badge badge-red" title="On legal hold — retention will not remove this alert at any age">
                        <span className="badge-dot" />Hold
                      </span>
                    )}
                  </div>
                  {canAck && (
                    <div className="tl-actions">
                      {a.state === 'new' && (
                        <button className="btn-subtle btn-sm" disabled={busy}
                                onClick={() => act(() => ackAlert(a.id), 'Acknowledged')}>
                          Acknowledge
                        </button>
                      )}
                      {a.state !== 'resolved' && (
                        <select className="tl-resolve" disabled={busy} value=""
                                aria-label="Resolve as"
                                title="Record what this turned out to be"
                                onChange={e => {
                                  const v = e.target.value as Resolution;
                                  if (v) act(() => resolveAlert(a.id, v), 'Resolved');
                                }}>
                          <option value="">Resolve…</option>
                          {VERDICTS.map(v => <option key={v.value} value={v.value}>{v.label}</option>)}
                        </select>
                      )}
                      <button className="iconbtn sm" disabled={busy}
                              aria-label={a.legal_hold ? 'Release the evidence hold' : 'Place an evidence hold'}
                              title={a.legal_hold ? 'Release the evidence hold' : 'Place an evidence hold'}
                              onClick={() => act(() => holdAlert(a.id, !a.legal_hold),
                                                 a.legal_hold ? 'Hold released' : 'Evidence hold placed')}>
                        {a.legal_hold ? '⚑' : '⚐'}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
