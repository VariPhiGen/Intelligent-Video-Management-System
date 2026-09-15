/**
 * aiEvents.ts (test) — an AI event as the API returns it, for component tests.
 *
 * Every field present, with realistic defaults, so a test states only what it
 * varies. Lives here rather than in a *.test file: importing a test file from
 * another would register its suites twice.
 */
import type { AiEvent } from '@/lib/aiEvents';

export function aiEvent(id: string, startedAt: string, over: Partial<AiEvent> = {}): AiEvent {
  return {
    id,
    camera: { id: 'c1', slug: 'cam2-o8iu', name: 'cam2' },
    activity: { key: 'restricted_zone_entry', label: 'Restricted zone entry', color: '#ffb020' },
    zone: 'Zone 1',
    started_at: startedAt,
    ended_at: null,
    duration_s: null,
    confidence: null,
    track_id: null,
    object_class: 'person',
    attributes: {},
    source: 'deepstream',
    playback: { camera: 'cam2-o8iu', start: Math.floor(Date.parse(startedAt) / 1000) - 10, end: 0 },
    ...over,
  };
}
