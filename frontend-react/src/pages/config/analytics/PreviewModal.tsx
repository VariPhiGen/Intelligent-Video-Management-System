/**
 * PreviewModal.tsx — human-readable summary of what a camera will detect, where
 * and when, over the exact payload that will be saved, with the raw contract
 * demoted behind a disclosure for the pipeline/integration user.
 */
import type { AnalyticsConfig, Camera } from '@/lib/types';
import { Modal } from '@/components/Modal';
import { entryArrow, hasEntryDirection } from '@/lib/tripwire';
import type { ActivityMeta } from './activities';
import { describeParams, type ActParams } from './analyticsConfig';

const GLYPH = { zone: '▱', tripwire: '⟋' } as const;

export function PreviewModal({ open, onClose, camera, config, byKey, totalEdited }: {
  open: boolean;
  onClose: () => void;
  camera: Camera;
  config: AnalyticsConfig;
  byKey: Record<string, ActivityMeta>;
  /** How many cards the operator has on screen, BEFORE the zone-less ones this
   *  payload drops — so the count line can say what it's actually showing
   *  instead of silently disagreeing with the card list. */
  totalEdited: number;
}) {
  const acts = config.activities;
  return (
    <Modal open={open} title="Configuration preview" width={640} onClose={onClose}>
      <div className="d-hint" style={{ marginTop: 0, marginBottom: acts.length ? 8 : 0 }}>
        What this camera will detect, where, and when — {acts.length} of {totalEdited} {totalEdited === 1 ? 'activity' : 'activities'} ready to save.
      </div>

      {acts.length === 0
        ? <div style={{ fontSize: 12.5, color: 'var(--dim)', padding: '6px 0' }}>Nothing configured yet — add an activity to get started.</div>
        : acts.map((a, i) => {
          const meta = byKey[a.type];
          const zoneIds = a.regions || [];
          return (
            <div key={a.type} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0', flexWrap: 'wrap',
                                     borderTop: i ? '1px solid var(--border)' : undefined }}>
              <span className="dot" style={{ background: meta?.color ?? 'var(--dim)', color: meta?.color ?? 'var(--dim)' }} />
              <b style={{ fontSize: 13 }}>{meta?.label ?? a.type}</b>
              <span style={{ fontSize: 12.5, display: 'inline-flex', alignItems: 'center', gap: 5, flexWrap: 'wrap' }}>
                {zoneIds.length
                  ? zoneIds.map(rid => {
                    const r = config.regions[rid];
                    if (!r) return null;
                    return (
                      <span key={rid} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
                        <span style={{ color: 'var(--dim)' }}>{GLYPH[r.kind]}</span>{r.name}
                        {r.kind === 'tripwire' && (
                          <span style={{ color: 'var(--dim)' }}>
                            {hasEntryDirection(r.direction) ? `· Entry ${entryArrow(r.points, r.direction) ?? ''}` : '· no entry direction'}
                          </span>
                        )}
                      </span>
                    );
                  })
                  : <span style={{ color: 'var(--red)' }}>no zones</span>}
              </span>
              <span style={{ color: 'var(--muted)', fontSize: 11.5, marginLeft: 'auto', fontFamily: 'var(--mono)' }}>
                {describeParams(a.params as ActParams, meta?.params_schema?.length ?? 0)}
              </span>
            </div>
          );
        })}

      <details style={{ marginTop: 14 }}>
        <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--muted)' }}>View raw config (JSON)</summary>
        <pre style={{ margin: '10px 0 0', padding: 14, fontSize: 11.5, lineHeight: 1.55, overflow: 'auto', maxHeight: 320,
                      background: 'var(--bg)', border: '1px solid var(--border)', borderRadius: 8,
                      color: 'var(--accent2)', fontFamily: 'var(--mono)' }}>
{JSON.stringify({ sensor_id: camera.slug, name: camera.name, uri: camera.local_rtsp_url,
                  enabled: camera.enabled, ...config }, null, 2)}
        </pre>
      </details>
    </Modal>
  );
}
