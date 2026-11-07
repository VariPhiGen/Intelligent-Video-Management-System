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

/**
 * Retention is minute-granular, so the trail cannot say "N days" any more. The
 * server records both the exact `retention_minutes` and a human `retention`
 * label; the log prints the label, because the question this row answers is
 * "how long did you keep it?" and "43200" is not that answer.
 */
describe('retention.changed', () => {
  function retention(detail: Record<string, any>): AuditEntry {
    return { action: 'retention.changed', target: 'cam34-n17v', detail } as AuditEntry;
  }

  it('prints the human label the server rendered', () => {
    expect(formatAction(retention({ retention_minutes: 20, retention: '20 minutes' })))
      .toBe('Retention changed · 20 minutes');
    expect(formatAction(retention({ retention_minutes: 43200, retention: '30 days' })))
      .toBe('Retention changed · 30 days');
  });

  it('falls back to the exact minutes when an entry carries no label', () => {
    // Entries written before the label was recorded still have to be readable:
    // the audit table is hash-chained, so nothing rewrites them.
    expect(formatAction(retention({ retention_minutes: 90 })))
      .toBe('Retention changed · 90 minutes');
  });

  it('says a reset to the default is a reset, not a period of zero', () => {
    expect(formatAction(retention({ retention_minutes: null })))
      .toBe('Retention changed · reset to default');
  });
});
