/**
 * How a camera's Smart Search change reads in the audit log.
 *
 * Until 2026-09-15 the entry said only "Camera config changed · <camera>" — the
 * same words for switching face collection ON as for switching it OFF. The
 * backend now records the before/after domains (pinned in
 * services/camera-mgmt/tests/test_search_audit.py); this is the half that makes
 * an administrator able to read it without opening the raw JSON.
 */
import { describe, expect, it } from 'vitest';
import type { AuditEntry } from '@/lib/types';
import { formatAction } from './AuditTab';

function entry(detail: Record<string, any>): AuditEntry {
  return {
    action: 'camera.config_changed', target: 'cam34-n17v', detail,
  } as AuditEntry;
}

describe('camera.config_changed', () => {
  it('names the domains before and after a search change', () => {
    expect(formatAction(entry({
      changed: ['search'],
      search: {
        before: { indexing: true, domains: ['person', 'vehicles'] },
        after: { indexing: true, domains: ['face', 'person', 'vehicles'] },
      },
    }))).toBe(
      'Camera config changed · search: person, vehicles → face, person, vehicles · cam34-n17v',
    );
  });

  it('says "off" when indexing stops, even though the domains are still stored', () => {
    expect(formatAction(entry({
      changed: ['search'],
      search: {
        before: { indexing: true, domains: ['person', 'vehicles'] },
        after: { indexing: false, domains: ['person', 'vehicles'] },
      },
    }))).toBe('Camera config changed · search: person, vehicles → off · cam34-n17v');
  });

  it('reads an older entry, with no search detail, exactly as before', () => {
    expect(formatAction(entry({ changed: ['search'] })))
      .toBe('Camera config changed · cam34-n17v');
  });
});
