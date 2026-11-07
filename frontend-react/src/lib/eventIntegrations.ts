import { apiFetch } from './api';
export type DestinationKind = 'webhook' | 'telegram' | 'slack' | 'kafka' | 'email' | 'sftp';
export interface Destination {
  id: string; name: string; kind: DestinationKind; config: Record<string, any>;
  enabled: boolean; media: boolean; allow_insecure: boolean; rate_per_min: number;
  max_age_s: number | null; has_secret: boolean; last_success_at: string | null;
  last_error_at: string | null; last_error: string | null;
}
export interface Subscription {
  id: string; destination_id: string; name: string; types: string[];
  filters: { cameras?: string[]; activities?: string[]; rules?: string[]; min_severity?: string; include_motion?: boolean };
  cooldown_s: number | null; enabled: boolean;
}
export interface Delivery {
  id: number; destination_id: string; destination_name: string; type: string;
  camera: { id: string; name: string }; state: string; attempts: number;
  created_at: string; last_error: string | null; media: string | null; message_id: string;
  provider_ref?: string | null;
}
export interface MailServer {
  configured: boolean; host?: string; port?: number; security?: 'starttls' | 'tls' | 'none'; verify_tls?: boolean;
  ca_pem?: string | null; username?: string | null; from_address?: string; from_name?: string | null; has_password: boolean;
}
export interface Controls { enabled: boolean; feed_enabled: boolean; environment_enabled: boolean; public_base_url: string; environment_base_url: string; retention_days: number }
export interface Options { cameras: { id: string; name: string }[]; activities: { id: string; name: string }[]; rules: { id: string; name: string }[] }
const write = <T,>(path: string, method: string, body?: unknown) => apiFetch<T>(path, { method, ...(body === undefined ? {} : { body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } }) });
export const listDestinations = () => apiFetch<Destination[]>('/event-destinations');
export const saveDestination = (id: string | null, body: unknown) => write<Destination>(`/event-destinations${id ? `/${id}` : ''}`, id ? 'PATCH' : 'POST', body);
export const deleteDestination = (id: string) => write(`/event-destinations/${id}`, 'DELETE');
export const testDestination = (id: string, include_live_frame = false, camera_id?: string) => write<{ ok: boolean; error?: string; media?: string; provider_ref?: string | null }>(`/event-destinations/${id}/test`, 'POST', { include_live_frame, ...(camera_id ? { camera_id } : {}) });
export const destinationStatus = (id: string) => apiFetch<{ queued: number; sent_24h: number; failed_24h: number }>(`/event-destinations/${id}/status`);
export const listSubscriptions = () => apiFetch<Subscription[]>('/event-subscriptions');
export const saveSubscription = (id: string | null, body: unknown) => write<Subscription>(`/event-subscriptions${id ? `/${id}` : ''}`, id ? 'PATCH' : 'POST', body);
export const deleteSubscription = (id: string) => write(`/event-subscriptions/${id}`, 'DELETE');
export const listDeliveries = (destination = '', state = '', before?: number) => apiFetch<{ items: Delivery[]; next: number | null }>(`/event-deliveries?${new URLSearchParams({ ...(destination ? { destination } : {}), ...(state ? { state } : {}), ...(before ? { before: String(before) } : {}) })}`);
export const retryDelivery = (id: number) => write(`/event-deliveries/${id}/retry`, 'POST');
export const getControls = () => apiFetch<Controls>('/event-integrations/settings');
export const saveControls = (enabled: boolean, feed_enabled: boolean, public_base_url?: string) => write<Controls>('/event-integrations/settings', 'PUT', { enabled, feed_enabled, ...(public_base_url === undefined ? {} : { public_base_url }) });
export const getOptions = () => apiFetch<Options>('/event-integrations/options');
export const getMailServer = () => apiFetch<MailServer>('/event-integrations/smtp');
export const saveMailServer = (body: unknown) => write<MailServer>('/event-integrations/smtp', 'PUT', body);
export const deleteMailServer = () => write<MailServer>('/event-integrations/smtp', 'DELETE');
export const fetchHostKey = (host: string, port: number) => write<{ fingerprint: string }>('/event-integrations/sftp-host-key', 'POST', { host, port });
