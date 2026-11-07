/**
 * rules.ts — automation rules and the alerts they raise.
 *
 * The API these call is deliberately shaped so that the dangerous thing is
 * hard to do by accident, and this client keeps that shape rather than
 * papering over it: `mode` is not part of a create or a patch, and arming goes
 * through its own call. A convenience wrapper that let a form PATCH `mode`
 * would undo the whole separation the backend exists to enforce.
 *
 * The three pure helpers at the bottom are what the tests point at. Each is a
 * sentence the screen tells an operator about what the automation will do, and
 * each has a wrong version that reads perfectly plausibly.
 */
import { apiFetch } from './api';

export type RuleMode = 'log_only' | 'armed';

export interface RuleAction {
  type: 'peripheral' | 'webhook';
  peripheral?: string;
  command?: string;
  for_seconds?: number | null;
  then?: string | null;
  /** Hold `command` until the triggering zone is empty, then send `then`.
   *  `for_seconds` is then only the safety cap. */
  until?: 'zone_empty' | null;
  grace_s?: number | null;
  url_ref?: string;
  /** What an `announce` plays on a Home Assistant speaker: a sound or TTS
   *  URL. Any key not named above rides to the driver as a service param. */
  media_content_id?: string;
}

export interface AlertRule {
  id: string;
  name: string;
  enabled: boolean;
  mode: RuleMode;
  activity: string;
  scope_kind: 'camera' | 'zone' | 'any';
  scope_value: string | null;
  min_confidence: number | null;
  conditions: Record<string, any>;
  cooldown_s: number;
  max_event_age_s: number;
  severity: 'info' | 'warning' | 'critical';
  actions: RuleAction[];
  active_hours: Record<string, any> | null;
  created_at: string;
  updated_at: string;
  created_by: string | null;
}

/** What a create accepts. No `mode` — see the module header. */
export interface RuleDraft {
  name: string;
  activity: string;
  scope_kind: 'camera' | 'zone' | 'any';
  scope_value?: string | null;
  min_confidence?: number | null;
  cooldown_s?: number;
  severity?: 'info' | 'warning' | 'critical';
  actions?: RuleAction[];
}

export interface ArmState {
  armed: boolean;
  by: string | null;
  at: string | null;
  reason: string | null;
  /** The API could not reach Valkey, so the state is UNKNOWN rather than off. */
  degraded: boolean;
}

export interface Alert {
  id: string;
  rule_id: string | null;
  rule_name: string | null;
  event_ids: string[];
  camera_id: string;
  severity: string;
  state: string;
  triggered_at: string;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution: string | null;
  resolution_note: string | null;
  notified: Record<string, string>;
  legal_hold: boolean;
}

export type Resolution = 'true_positive' | 'false_positive' | 'duplicate' | 'no_action';

// ── Rules ────────────────────────────────────────────────────────────────────

export const listRules = () => apiFetch<AlertRule[]>('/rules');

export const createRule = (draft: RuleDraft) =>
  apiFetch<AlertRule>('/rules', { method: 'POST', body: JSON.stringify(draft) });

export const patchRule = (id: string, patch: Partial<RuleDraft> & { enabled?: boolean }) =>
  apiFetch<AlertRule>(`/rules/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });

export const deleteRule = (id: string) =>
  apiFetch<{ deleted: string }>(`/rules/${id}`, { method: 'DELETE' });

/** Its own call, never a patch: this is the moment a rule can move something. */
export const armRule = (id: string) =>
  apiFetch<AlertRule>(`/rules/${id}/arm`, { method: 'POST', body: '{}' });

export const disarmRule = (id: string) =>
  apiFetch<AlertRule>(`/rules/${id}/disarm`, { method: 'POST', body: '{}' });

// ── The engine-wide kill switch ──────────────────────────────────────────────

export const armState = () => apiFetch<ArmState>('/rules/arm-state');

/** `reason` is required by the API and shown to every operator in the banner. */
export const disarmEngine = (reason: string) =>
  apiFetch<ArmState>('/rules/disarm', { method: 'POST', body: JSON.stringify({ reason }) });

export const rearmEngine = (reason: string) =>
  apiFetch<ArmState>('/rules/rearm', { method: 'POST', body: JSON.stringify({ reason }) });

// ── Alerts ───────────────────────────────────────────────────────────────────

export const listAlerts = (params: { state?: string; camera?: string; limit?: number } = {}) => {
  const q = new URLSearchParams();
  if (params.state) q.set('state', params.state);
  if (params.camera) q.set('camera', params.camera);
  q.set('limit', String(params.limit ?? 100));
  return apiFetch<{ total: number; alerts: Alert[] }>(`/alerts?${q}`);
};

export const ackAlert = (id: string) =>
  apiFetch<Alert>(`/alerts/${id}/ack`, { method: 'POST', body: '{}' });

export const resolveAlert = (id: string, resolution: Resolution, note?: string) =>
  apiFetch<Alert>(`/alerts/${id}/resolve`, {
    method: 'POST', body: JSON.stringify({ resolution, note: note || null }),
  });

export const holdAlert = (id: string, legal_hold: boolean) =>
  apiFetch<Alert>(`/alerts/${id}/hold`, {
    method: 'POST', body: JSON.stringify({ legal_hold }),
  });

// ── What the screen says ─────────────────────────────────────────────────────

/**
 * The mode column. "Disabled" would be a lie: a log_only rule fires, matches
 * and raises alerts — it just touches nothing.
 *
 * An unrecognised mode renders as ITSELF. Falling back to "Log only" would put
 * the safe label on a rule that might not be, which is the one direction this
 * must never fail in.
 */
export function modeLabel(mode: RuleMode | string): string {
  if (mode === 'armed') return 'Armed';
  if (mode === 'log_only') return 'Log only';
  return String(mode);
}

/** May this principal arm this rule? False while `me` is still loading: an
 *  optimistically-offered button turns into a 403 the operator reads as a
 *  broken product. */
export function canArm(caps: Record<string, boolean> | undefined | null,
                       mode: RuleMode | string): boolean {
  return !!caps?.rule_manage && mode !== 'armed';
}

/**
 * The banner above the page. Empty string means "say nothing".
 *
 * `degraded` is checked FIRST and reported as its own thing. "Alice switched
 * it off" and "we could not reach Valkey to find out" call for different
 * actions, and a banner that collapses them sends an operator looking in the
 * wrong place.
 */
export function armBannerText(s: ArmState): string {
  if (s.degraded) {
    return 'Automation state could not be confirmed — no rule will act until it is. '
         + 'Alerts are still being raised.';
  }
  if (s.armed) return '';
  const who = s.by ? ` by ${s.by}` : '';
  const why = s.reason ? ` — ${s.reason}` : '';
  // Said every time, because disarm stops ACTUATION and never evidence. An
  // operator who reads this as "the system stopped watching" goes looking for
  // a second failure that is not there.
  return `Automation is disarmed${who}${why}. Alerts are still being raised.`;
}
