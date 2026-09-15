/** The banner claims something about the running system, so these tests are
 *  about agreement with the BACKEND's rule, not about the banner's styling.
 *
 *  Two of the three cases below are regressions with a measured symptom: on
 *  2026-09-07 a disabled camera showed "AI is still analysing this camera"
 *  while the smartsearch registry had already dropped it, and every camera on
 *  the appliance rendered "Smart Search still run" because the verb only
 *  agreed in the plural branch.
 */
import { describe, it, expect } from 'vitest';
import { aiStillOn, searchIndexingActive, motionActive, aiRunningPhrase,
         type AiCamera } from './aiStillOn';

const cam = (over: Partial<AiCamera> = {}): AiCamera => ({
  enabled: true,
  recording: false,
  search_indexing: true,
  motion_detection: false,
  search_domains: ['person', 'vehicles'],
  ...over,
});

describe('aiStillOn', () => {
  it('fires when recording is off but indexing continues', () => {
    expect(aiStillOn(cam())).toBe(true);
  });

  it('stays silent while the camera is still recording', () => {
    expect(aiStillOn(cam({ recording: true }))).toBe(false);
  });

  it('stays silent on a DISABLED camera — nothing is analysing it', () => {
    // The backend indexes iff enabled AND opted in AND has domains, and the
    // registry drops a disabled camera. Claiming otherwise offers to stop
    // something that already stopped.
    expect(aiStillOn(cam({ enabled: false }))).toBe(false);
    expect(aiStillOn(cam({ enabled: false, motion_detection: true }))).toBe(false);
  });

  it('stays silent when no domain is selected — stored as off', () => {
    expect(aiStillOn(cam({ search_domains: [] }))).toBe(false);
  });

  it('fires on motion alone, with indexing opted out', () => {
    expect(aiStillOn(cam({ search_indexing: false, motion_detection: true }))).toBe(true);
  });

  it('treats a missing opt-in as on, because indexing defaults to on', () => {
    const { search_indexing: _drop, ...rest } = cam();
    expect(aiStillOn(rest as AiCamera)).toBe(true);
  });
});

describe('searchIndexingActive / motionActive', () => {
  it('both require the camera to be enabled', () => {
    expect(searchIndexingActive(cam({ enabled: false }))).toBe(false);
    expect(motionActive(cam({ enabled: false, motion_detection: true }))).toBe(false);
  });
});

describe('aiRunningPhrase', () => {
  it('is singular when only Smart Search runs — the common case here', () => {
    expect(aiRunningPhrase(cam())).toBe('Smart Search indexing still runs');
  });

  it('is plural only when both run', () => {
    expect(aiRunningPhrase(cam({ motion_detection: true })))
      .toBe('Smart Search indexing and motion detection still run');
  });

  it('names motion alone rather than crediting Smart Search', () => {
    expect(aiRunningPhrase(cam({ search_indexing: false, motion_detection: true })))
      .toBe('Motion detection still runs');
  });

  it('never leaves a subject-verb disagreement in any reachable state', () => {
    for (const search of [true, false]) {
      for (const motion of [true, false]) {
        const c = cam({ search_indexing: search, motion_detection: motion });
        if (!aiStillOn(c)) continue;
        const phrase = aiRunningPhrase(c);
        const plural = phrase.includes(' and ');
        expect(phrase.endsWith(plural ? 'run' : 'runs')).toBe(true);
      }
    }
  });
});
