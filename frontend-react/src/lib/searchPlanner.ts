import { apiFetch } from './api';
import type { PersonHit, VehicleHit, Withheld } from './smartsearch';

export type PlannerProvider = 'qwen' | 'openai' | 'anthropic';
export type ReasoningEffort = 'low' | 'default';
export interface PlannerProfile { model: string; base_url: string; reasoning_effort: ReasoningEffort }
export interface PlannerConfig {
  enabled: boolean;
  assistant_enabled?: boolean;
  provider: PlannerProvider;
  profiles: Record<PlannerProvider, PlannerProfile>;
  point_minutes: number;
  lookback_hours: number;
  max_window_days: number;
  timeout_seconds: number;
}
export interface PlannerSettings {
  config: PlannerConfig;
  key_configured: Record<PlannerProvider, boolean>;
  tested: boolean;
  assistant_tested?: boolean;
}
export interface PlannerStatus { enabled: boolean; provider: PlannerProvider; ready: boolean; assistant_enabled?: boolean; assistant_ready?: boolean; available_tools?: string[] }
export type NaturalResult = {
  status: 'results'; domain: 'people' | 'vehicles'; summary: string;
  outcome: { results: (PersonHit | VehicleHit)[]; withheld: Withheld; matched: number; truncated: boolean };
} | { status: 'clarification' | 'unsupported'; message: string; choices?: string[] };

export const plannerStatus = () => apiFetch<PlannerStatus>('/search/planner/status');
export const plannerSettings = () => apiFetch<PlannerSettings>('/search/planner/settings');
export const savePlanner = (config: PlannerConfig, keys: Partial<Record<PlannerProvider, string>>) =>
  apiFetch<PlannerSettings>('/search/planner/settings', { method: 'PUT', body: JSON.stringify({ config, keys }) });
export const testPlanner = (config: PlannerConfig, keys: Partial<Record<PlannerProvider, string>>) =>
  apiFetch<{ ok: boolean }>('/search/planner/test', { method: 'POST', body: JSON.stringify({ config, keys }) });
export const naturalSearch = (body: Record<string, unknown>, signal?: AbortSignal) =>
  apiFetch<NaturalResult>('/search/natural', { method: 'POST', body: JSON.stringify(body), signal });

export const testAssistant = (config: PlannerConfig, keys: Partial<Record<PlannerProvider, string>>) =>
  apiFetch<{ ok: boolean }>('/search/planner/assistant/test', { method: 'POST', body: JSON.stringify({ config, keys }) });
