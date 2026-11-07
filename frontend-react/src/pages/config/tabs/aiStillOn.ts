/** Is AI still analysing a camera whose recording has been switched off?
 *
 *  Extracted from RecordingTab so the predicate is reachable by a test: it is
 *  three-valued in a way that is easy to get wrong, and the first version got
 *  it wrong by checking only the opt-in flags.
 *
 *  The rule has to match the BACKEND's, which is the thing that actually
 *  decides (routers/cameras.py): a camera is indexed iff it is **enabled** AND
 *  opted in AND has at least one domain — an empty domain set is stored as
 *  "off". Omitting `enabled` made the banner claim AI was running on a
 *  disabled camera, which the smartsearch registry had already dropped; the
 *  operator was told to stop something that had already stopped.
 */
import type { Camera } from '@/lib/types';

/** Only the fields the decision depends on, so a test states nothing else. */
export type AiCamera = Pick<Camera,
  'enabled' | 'recording' | 'search_indexing' | 'motion_detection' | 'search_domains' | 'timelapse_enabled'>;

/** `=== true`, not `!== false`: indexing is an opt-IN (migration 038), so a
 *  payload that omits the field must not read as opted in. It was the other way
 *  round while indexing defaulted on, and the reason is the same both times —
 *  the banner must never claim AI is running on a camera nobody switched on. */
export function searchIndexingActive(c: AiCamera): boolean {
  return c.enabled === true
    && c.search_indexing === true
    && (c.search_domains?.length ?? 0) > 0;
}

export function motionActive(c: AiCamera): boolean {
  return c.enabled === true && c.motion_detection === true;
}

export function aiStillOn(c: AiCamera): boolean {
  return c.recording === false && (searchIndexingActive(c) || motionActive(c));
}

/** Name only what is actually running. The banner can be raised by motion
 *  alone, so a sentence hard-coded to "Smart Search" would be false in exactly
 *  the case where the operator most needs to know which service to look at. */
export function aiRunningPhrase(c: AiCamera): string {
  const search = searchIndexingActive(c);
  const motion = motionActive(c);
  if (search && motion) return 'Smart Search indexing and motion detection still run';
  if (motion) return 'Motion detection still runs';
  return 'Smart Search indexing still runs';
}

/** Time-lapse is NOT AI and the AI banner's "Stop AI" does not touch it, so it
 *  gets its own notice — but it answers the same question the banner exists for:
 *  "I switched recording off; is anything still keeping pictures of this view?"
 *  Stills come from the live relay, so the answer is yes while it is on. Same
 *  `=== true` rule as indexing: an absent flag is off (migration 057 default). */
export function timelapseStillOn(c: AiCamera): boolean {
  return c.recording === false && c.enabled === true && c.timelapse_enabled === true;
}
