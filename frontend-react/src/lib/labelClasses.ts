/**
 * labelClasses.ts — the fleet label taxonomy.
 *
 * Administered under Administration → Label classes (admin-only, server-side);
 * read from Playback so the annotate bar can offer a picker. The `key` is what
 * annotations name and what COCO exports carry, so it is created once and
 * never renamed — only `label` is a display concern.
 */
import { apiFetch } from '@/lib/api';

export interface LabelClass {
  key: string;
  label: string;
  color: string;
  sort_order: number;
  active: boolean;
  description: string | null;
  annotation_count: number;
}

export const listLabelClasses = () =>
  apiFetch<LabelClass[]>('/label-classes');

export const createLabelClass = (body: {
  key: string; label: string; color: string;
  sort_order?: number; description?: string | null;
}) =>
  apiFetch<LabelClass>('/label-classes', {
    method: 'POST', body: JSON.stringify(body),
  });

export const updateLabelClass = (key: string, body: Partial<LabelClass>) =>
  apiFetch<LabelClass>(`/label-classes/${encodeURIComponent(key)}`, {
    method: 'PATCH', body: JSON.stringify(body),
  });

export const deleteLabelClass = (key: string) =>
  apiFetch<void>(`/label-classes/${encodeURIComponent(key)}`, {
    method: 'DELETE',
  });
