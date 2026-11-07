import { apiFetch } from './api';
import type { SearchCamera } from './smartsearch';

export interface AssistantItem {
  id: string; camera: SearchCamera; when_ms?: number; started_at?: string;
  activity?: { key: string; label: string }; plate?: string; score?: number;
  bbox?: number[] | null; has_frame?: boolean;
  playback?: { camera: string; start: number };
}
export interface Collection {
  tool: string;
  scope: { cameras?: string[] | null; activities?: string[] | null; appearance?: string; plate?: string;
    time_from: string; time_to: string; timezone: string; grouping?: string };
  /** Display names for the chips; `scope` keeps the slugs and keys. Absent on older investigations. */
  scope_labels?: { cameras?: string[] | null; activities?: string[] | null };
  items: AssistantItem[]; count?: number; count_as_of?: string; groups?: { label: string; count: number }[];
  interpretations?: string[]; warnings?: string[]; next_cursor?: string | null; truncated?: boolean; error?: string;
  setup?: { camera: SearchCamera; activities: string[]; recording_enabled: boolean;
    retention_minutes: number; recorded_seconds?: number | null; window_seconds?: number; search_indexing?: boolean; recorder_available: boolean; retained_window_ms?: number[] | null }[];
}
export interface AssistantAnswer {
  status: 'results' | 'clarification' | 'unsupported' | 'partial' | 'error'; message: string;
  collections: Collection[]; choices?: { id: string; label: string }[]; selected_choice?: string;
  playback?: { camera: string; start: number };
}
export interface Conversation {
  id: string; version: number; timezone: string; progress?: string; suggestions?: string[];
  exchanges: { request_id: string; message: string; answer: AssistantAnswer }[];
  answer?: AssistantAnswer;
}
const root = '/search/assistant/conversations';
export const createInvestigation = (timezone: string) => apiFetch<Conversation>(root, { method: 'POST', body: JSON.stringify({ timezone }) });
export const restoreInvestigation = (id: string) => apiFetch<Conversation>(`${root}/${id}`);
export const clearInvestigation = (id: string) => apiFetch(`${root}/${id}`, { method: 'DELETE' });
export const assistantTurn = (id: string, body: { message: string; request_id: string; expected_version: number;
  choice_id?: string; remove_filter?: string; result_id?: string; result_action?: 'play' | 'similar'; timezone?: string }) => apiFetch<Conversation>(`${root}/${id}/turns`, { method: 'POST', body: JSON.stringify(body) });

export const investigationProgress = (id: string) => apiFetch<{ progress: string | null }>(`${root}/${id}/progress`);
