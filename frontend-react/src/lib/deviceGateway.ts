/**
 * deviceGateway.ts — device-gateway URLs, and what a binding's health says.
 *
 * The gateway (services/device-gateway) puts one URL in front of each device:
 * POST switches it, GET is a read-only health check. A Webhook binding to such
 * a URL carries `config.health = true`, and the bridge then GETs it on every
 * verify tick, so the wall can say "healthy" or "modbus-tcp/timeout" rather
 * than only "the address is well-formed".
 *
 * Pure functions, so the URL rules and the wording are testable without a page.
 */
import type { Binding } from './bridge';

export const DEFAULT_GATEWAY = 'http://127.0.0.1:8099';

export type GatewayKind = 'relay' | 'speaker';

export interface RelayTarget { kind: 'relay'; host: string; port: number; channel: number; slave: number }
export interface SpeakerTarget {
  kind: 'speaker'; group: string; port: number;
  /** The speaker's own IP, for a reachability probe. Multicast cannot answer. */
  probe?: string; sound?: string; iface?: string;
}
export type GatewayTarget = RelayTarget | SpeakerTarget;

export const PROTOCOL_LABEL: Record<GatewayKind, string> = {
  relay: 'Modbus TCP relay',
  speaker: 'Multicast speaker',
};

export function buildGatewayUrl(base: string, t: GatewayTarget): string {
  const root = (base.trim() || DEFAULT_GATEWAY).replace(/\/+$/, '');
  if (t.kind === 'relay') {
    const port = t.port && t.port !== 4196 ? `:${t.port}` : '';
    const slave = t.slave && t.slave !== 1 ? `?slave=${t.slave}` : '';
    return `${root}/relay/${t.host.trim()}${port}/${t.channel}${slave}`;
  }
  const q = new URLSearchParams();
  if (t.probe?.trim()) q.set('probe', t.probe.trim());
  if (t.sound?.trim()) q.set('sound', t.sound.trim());
  if (t.iface?.trim()) q.set('if', t.iface.trim());
  const qs = q.toString();
  return `${root}/speaker/${t.group.trim()}:${t.port}${qs ? `?${qs}` : ''}`;
}

/** The base and target of a gateway URL, or null for any other URL. */
export function parseGatewayUrl(url: string): { base: string; target: GatewayTarget } | null {
  let u: URL;
  try { u = new URL(url); } catch { return null; }
  const parts = u.pathname.split('/').filter(Boolean).map(decodeURIComponent);
  const i = parts.findIndex(p => p === 'relay' || p === 'speaker');
  if (i < 0) return null;
  const base = `${u.origin}${parts.length && i > 0 ? '/' + parts.slice(0, i).join('/') : ''}`;
  const q = u.searchParams;
  if (parts[i] === 'relay' && parts.length === i + 3) {
    const [host, port] = parts[i + 1].split(':');
    const channel = Number(parts[i + 2]);
    if (!host || !Number.isInteger(channel)) return null;
    return { base, target: { kind: 'relay', host, port: Number(port) || 4196, channel,
                             slave: Number(q.get('slave')) || 1 } };
  }
  if (parts[i] === 'speaker' && parts.length === i + 2) {
    const [group, port] = parts[i + 1].split(':');
    if (!group || !Number(port)) return null;
    return { base, target: { kind: 'speaker', group, port: Number(port),
                             probe: q.get('probe') ?? undefined, sound: q.get('sound') ?? undefined,
                             iface: q.get('if') ?? undefined } };
  }
  return null;
}

export type HealthTone = 'green' | 'red' | 'amber' | 'dim';
export interface Health { tone: HealthTone; label: string; detail?: string }

/** One line for the tile: is this device answering, and over what. */
export function healthOf(binding: Binding | null | undefined, now: Date = new Date()): Health | null {
  if (!binding) return null;
  const gw = binding.driver === 'webhook' ? parseGatewayUrl(binding.address) : null;
  const via = gw ? PROTOCOL_LABEL[gw.target.kind] : binding.driver;
  const checked = binding.verified_at ? ago(binding.verified_at, now) : null;
  if (binding.driver === 'webhook' && binding.config?.health) {
    if (binding.verify_error?.startsWith('unhealthy:')) {
      return { tone: 'red', label: `Unhealthy · ${via}`,
               detail: binding.verify_error.slice('unhealthy:'.length)
                 + (checked ? ` · last healthy ${checked}` : '') };
    }
    if (binding.verify_error) {
      return { tone: 'amber', label: `Not checked · ${via}`, detail: binding.verify_error };
    }
    if (binding.verified_at) return { tone: 'green', label: `Healthy · ${via}`, detail: `checked ${checked}` };
    return { tone: 'dim', label: `Checking · ${via}`, detail: 'first health check pending' };
  }
  // Every other binding is verified by address only: say so, rather than
  // letting a green dot imply the device itself answered.
  if (binding.verify_error) return { tone: 'amber', label: `Address problem · ${via}`, detail: binding.verify_error };
  if (binding.verified_at) return { tone: 'dim', label: `Address OK · ${via}`, detail: 'device health not checked' };
  return { tone: 'dim', label: `Not verified yet · ${via}` };
}

function ago(iso: string, now: Date): string {
  const s = Math.max(0, Math.round((now.getTime() - new Date(iso).getTime()) / 1000));
  if (Number.isNaN(s)) return 'at an unknown time';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return new Date(iso).toLocaleString();
}
