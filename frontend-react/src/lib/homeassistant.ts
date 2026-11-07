/**
 * homeassistant.ts — the VMS side of a Home Assistant connection.
 *
 * VMS OWNS THE RULES, HA ONLY EXECUTES. The browser never talks to HA's API:
 * everything here goes through /api/ha on the VMS, which holds the token. The
 * one exception is a plain LINK for a person — "add a device in Home
 * Assistant" — because adding a Shelly or a Hue bridge is HA's own screen and
 * rebuilding it here would be rebuilding Home Assistant.
 */
import { apiFetch } from './api';

export type HaState = 'connected' | 'offline' | 'not_configured';

export interface HaConnection {
  /** ui — saved here; env — HA_URL/HA_TOKEN; none — nothing configured. */
  source: 'ui' | 'env' | 'none';
  url: string | null;
  token_set: boolean;
  state: HaState;
  /** unreachable / unauthorized / http_<code> */
  error: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface HaEntity {
  entity_id: string;
  domain: string;
  name: string;
  state: string | null;
  /** The category an import defaults to. */
  category: string | null;
  /** Slug of the peripheral already bound to this entity, if any. */
  bound_to: string | null;
}

export type ImportResult =
  | { entity_id: string; result: 'imported'; peripheral: string }
  | { entity_id: string; result: 'already_bound'; peripheral: string }
  | { entity_id: string; result: 'name_taken'; name: string }
  | { entity_id: string; result: 'not_found' };

export interface ImportItem {
  entity_id: string;
  name?: string;
  category?: string;
  location?: string;
}

export const getConnection = () => apiFetch<HaConnection>('/ha/connection');

export const saveConnection = (url: string, token?: string) =>
  apiFetch<HaConnection>('/ha/connection', {
    method: 'PUT',
    // Blank means KEEP the stored token, never "clear it" — the token never
    // comes back to the page, so a blank field is the normal state of an edit.
    body: JSON.stringify(token ? { url, token } : { url }),
  });

export const testConnection = (url?: string, token?: string) =>
  apiFetch<{ state: HaState; error: string | null }>('/ha/connection/test', {
    method: 'POST',
    body: url ? JSON.stringify(token ? { url, token } : { url }) : undefined,
  });

export const forgetConnection = () =>
  apiFetch<HaConnection>('/ha/connection', { method: 'DELETE' });

export const listEntities = () => apiFetch<HaEntity[]>('/ha/entities');

export const importEntities = (items: ImportItem[]) =>
  apiFetch<{ imported: number; results: ImportResult[] }>('/ha/import', {
    method: 'POST', body: JSON.stringify({ items }),
  });

// ── What the panel says ──────────────────────────────────────────────────────

export function connectionLabel(c: Pick<HaConnection, 'state' | 'error'>): string {
  if (c.state === 'connected') return 'Connected';
  if (c.state === 'not_configured') return 'Not connected';
  switch (c.error) {
    case 'unauthorized': return 'Token rejected — create a new one in Home Assistant';
    case 'unreachable': return 'Home Assistant is not reachable';
    default: return c.error ? `Offline (${c.error})` : 'Offline';
  }
}

export function importResultLabel(r: ImportResult): string {
  switch (r.result) {
    case 'imported': return 'imported';
    case 'already_bound': return `already imported as ${r.peripheral}`;
    case 'name_taken': return `a device named "${r.name}" already exists`;
    case 'not_found': return 'no longer in Home Assistant';
  }
}

/** HA as the PERSON at this browser reaches it.
 *
 *  The VMS reaches HA on 127.0.0.1 (both run on the appliance), and that
 *  address in a link would point at the operator's own laptop. A loopback
 *  host is swapped for the host this page was loaded from; anything else is
 *  already reachable as configured.
 */
export function browserUrl(haUrl: string | null, pageHost: string): string | null {
  if (!haUrl) return null;
  let u: URL;
  try { u = new URL(haUrl); } catch { return null; }
  if (['127.0.0.1', 'localhost', '::1', '[::1]'].includes(u.hostname)) {
    u.hostname = pageHost;
  }
  return u.toString().replace(/\/$/, '');
}

/** Where to send someone to add a device in HA. */
export const addDeviceUrl = (base: string) => `${base}/config/integrations/dashboard`;
