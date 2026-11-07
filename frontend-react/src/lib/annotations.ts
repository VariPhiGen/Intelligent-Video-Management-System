/**
 * annotations.ts — operator-drawn boxes, indexed.
 *
 * The pixels live on the NVR; these are the facts about them. Browsing and
 * exporting the pile is gated on `data_collection`; fetching one frame back is
 * gated on `playback_search`, because that is the frame the operator drew on.
 */
import { apiFetch } from '@/lib/api';

export interface Annotation {
  id: string;
  camera_slug: string;
  class_key: string;
  captured_at: string;
  bbox: { x: number; y: number; width: number; height: number };
  frame: { width: number; height: number };
  image_path: string;
  dataset_id: number | null;
  expires_at: string | null;
  /** Why this one is kept past its deadline. Null alongside a null
   *  `expires_at` means a dataset owns it. */
  kept_reason: string | null;
  created_by: string | null;
}

export interface AnnotationPage { annotations: Annotation[]; total: number }
export interface AnnotationStats {
  by_class: Record<string, number>;
  /** Cameras that HAVE annotations, which is not the camera registry: 047 keeps
   *  `camera_slug` out of a foreign key so an annotation outlives its camera.
   *  This is what the camera filter is built from. */
  by_camera: Record<string, number>;
  total: number;
}

export const listAnnotations = (params: Record<string, string> = {}) => {
  const q = new URLSearchParams(params).toString();
  return apiFetch<AnnotationPage>(`/annotations${q ? `?${q}` : ''}`);
};

export const annotationStats = () =>
  apiFetch<AnnotationStats>('/annotations/stats');

/** Relabel, and/or decide whether it is kept past its deadline.
 *  Relabelling needs `playback_search` (fixing your own misclick); clearing
 *  the expiry additionally needs `data_collection`. */
export const updateAnnotation = (
  id: string,
  body: { class_key?: string; keep?: boolean; kept_reason?: string },
) =>
  apiFetch<Annotation>(`/annotations/${encodeURIComponent(id)}`, {
    method: 'PATCH', body: JSON.stringify(body),
  });

export const deleteAnnotation = (id: string) =>
  apiFetch<void>(`/annotations/${encodeURIComponent(id)}`, { method: 'DELETE' });

export const exportAnnotations = (body: Record<string, unknown>) =>
  apiFetch<{ coco: unknown; s3_key: string | null }>('/annotations/export', {
    method: 'POST', body: JSON.stringify(body),
  });

/** The frame itself, streamed through from the NVR. */
export const annotationImageUrl = (id: string) =>
  `/api/annotations/${encodeURIComponent(id)}/image`;
