import { apiFetch, apiUpload } from './api';

/** The pipeline's own JSON, relayed untouched — its shape is DeepStream's. */
export type PipelineAnswer = unknown;

export interface ModelUpload {
  name: string;
  onnx: File;
  labels: File;
  batchSize: number;
  /** Omitted → the pipeline's own default. */
  networkSize: number | null;
  restart: boolean;
}

export interface DeepstreamSettings {
  /** Saved address; null = using the environment default. */
  api_url: string | null;
  /** The address calls actually go to right now. */
  effective_url: string;
  /** DEEPSTREAM_API_URL from the server's environment. */
  default_url: string;
  updated_by: string | null;
  updated_at: string | null;
}

export const getDeepstreamSettings = () => apiFetch<DeepstreamSettings>('/deepstream/settings');
export const saveDeepstreamSettings = (api_url: string | null) =>
  apiFetch<DeepstreamSettings>('/deepstream/settings', { method: 'PUT', body: JSON.stringify({ api_url }) });
export const testDeepstreamSettings = (api_url: string | null) =>
  apiFetch<{ ok: boolean; url: string; health: unknown }>('/deepstream/settings/test',
    { method: 'POST', body: JSON.stringify({ api_url }) });

export const listDeepstreamModels = () => apiFetch<PipelineAnswer>('/deepstream/models');

export function addDeepstreamModel(m: ModelUpload) {
  const form = new FormData();
  form.append('name', m.name);
  form.append('onnx', m.onnx);
  form.append('labels', m.labels);
  form.append('batch_size', String(m.batchSize));
  if (m.networkSize) form.append('network_size', String(m.networkSize));
  form.append('restart', m.restart ? 'true' : 'false');
  return apiUpload<PipelineAnswer>('/deepstream/models', form);
}

export const restartDeepstream = () =>
  apiFetch<PipelineAnswer>('/deepstream/models/restart', { method: 'POST' });

export interface ModelList {
  rows: Record<string, unknown>[];
  /** Name of the model the pipeline is running, when it says. */
  active: string | null;
}

const ACTIVE_KEYS = ['active', 'current', 'active_model', 'current_model', 'loaded', 'running'];
const LIST_KEYS = ['models', 'available', 'items', 'data'];

const isObj = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

/** Pull model rows out of whatever shape the pipeline answers with:
 *  a bare array (of objects or names), an array under `models`/`available`/…,
 *  or a map of name → details. Returns null when nothing looks like a list. */
export function modelRows(answer: PipelineAnswer): ModelList | null {
  const fromArray = (v: unknown[]) =>
    v.map(x => (isObj(x) ? x : { name: x }));
  const fromMap = (m: Record<string, unknown>) =>
    Object.entries(m).map(([name, v]) => (isObj(v) ? { name, ...v } : { name, value: v }));

  if (Array.isArray(answer)) return { rows: fromArray(answer), active: null };
  if (!isObj(answer)) return null;

  const activeRaw = ACTIVE_KEYS.map(k => answer[k]).find(v => v !== undefined && v !== null);
  const active = typeof activeRaw === 'string' ? activeRaw
    : isObj(activeRaw) && typeof activeRaw.name === 'string' ? activeRaw.name : null;

  let rows: Record<string, unknown>[] | null = null;
  for (const k of LIST_KEYS) {
    const v = answer[k];
    if (Array.isArray(v)) { rows = fromArray(v); break; }
    if (isObj(v)) { rows = fromMap(v); break; }
  }
  if (!rows) {
    const firstArray = Object.values(answer).find(Array.isArray);
    if (firstArray) rows = fromArray(firstArray);
  }
  if (!rows) {
    // A map of name → details at the top level: every non-active value is an object.
    const entries = Object.entries(answer).filter(([k]) => !ACTIVE_KEYS.includes(k));
    if (entries.length && entries.every(([, v]) => isObj(v))) rows = fromMap(Object.fromEntries(entries));
  }
  if (!rows) return null;
  // Flag the running model so the table can show it, when the rows don't already.
  if (active) rows = rows.map(r => ('active' in r ? r : { ...r, active: r.name === active }));
  return { rows, active };
}
