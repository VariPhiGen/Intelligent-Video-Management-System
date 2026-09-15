/**
 * StepAssign.tsx — wizard step 3: batch fields (zone / prefix / lawful basis /
 * installation date / GPS / recording default), the "Ready to add" list with
 * per-row editable names and the batch add (wzApplyPrefix / wzRenderAddRows /
 * wzAddSelected), plus the advanced full devices table (dscRenderChips /
 * dscRenderTable / dscVerify / dscIgnore / dscDelete / dscOpenAdd /
 * dscSubmitAdd).
 */
import { useEffect, useMemo, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { apiBlob, apiFetch } from '@/lib/api';
import { Modal } from '@/components/Modal';
import { useToast } from '@/components/Toast';
import type { SitemapMeta } from '@/lib/types';
import { CredsModal } from './StepCredentials';
import { parseGps } from '../map/mapHelpers';
import {
  DSC_META, LAWFUL_BASES, bestProfile, complianceReady, defaultName, dscIsCamera,
  numberedName, wzCompliance, wzMeta,
  type AssignState, type Device,
} from './useDiscovery';

import { AddModal } from './AddDeviceModal';
import { DevicesTable } from './DevicesTable';
import { MapPlacePreview } from './MapPlacePreview';

interface RowStatus { text: string; color: string }

export function StepAssign({ devices, loadDevices, assign, setAssign, zones, onAdded, refreshCameras }: {
  devices: Device[];
  loadDevices: () => Promise<void>;
  assign: AssignState;
  setAssign: Dispatch<SetStateAction<AssignState>>;
  zones: string[];
  onAdded: (name: string, id: string) => void;
  refreshCameras: () => void;
}) {
  const toast = useToast();
  const ready = useMemo(() => devices.filter(d => d.status === 'verified'), [devices]);
  const already = useMemo(() => devices.filter(d => d.status === 'added'), [devices]);

  const [names, setNames] = useState<Record<string, string>>({});
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  const [rowStatus, setRowStatus] = useState<Record<string, RowStatus>>({});
  const [adding, setAdding] = useState(false);
  const [credDev, setCredDev] = useState<Device | null>(null);
  const [addDev, setAddDev] = useState<Device | null>(null);
  const [sitemaps, setSitemaps] = useState<SitemapMeta[]>([]);

  // The rows that will actually be submitted (checkbox-filtered) — shared by
  // addSelected and the map-placement single-vs-multiple decision so the two
  // can never disagree.
  const selected = useMemo(() => ready.filter(d => checks[d.id] !== false), [ready, checks]);

  // Sitemap list for the "Place on map" block — fetched once; hidden entirely
  // if the call fails or there are no sitemaps to place cameras on.
  useEffect(() => {
    apiFetch<SitemapMeta[]>('/sitemaps').then(setSitemaps).catch(() => setSitemaps([]));
  }, []);

  // Seed names for devices that just became verified (prefix numbering, else
  // the legacy default "Vendor Model IP").
  useEffect(() => {
    setNames(prev => {
      const next = { ...prev };
      ready.forEach((d, i) => {
        if (next[d.id] === undefined) {
          next[d.id] = assign.prefix.trim() ? numberedName(assign.prefix.trim(), i) : defaultName(d);
        }
      });
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready]);

  // Legacy wzApplyPrefix — live renumbering of EVERY row on prefix change.
  function applyPrefix(v: string) {
    setAssign(a => ({ ...a, prefix: v }));
    const p = v.trim();
    setNames(prev => {
      const next = { ...prev };
      ready.forEach((d, i) => { next[d.id] = p ? numberedName(p, i) : defaultName(d); });
      return next;
    });
  }

  const setStatus = (id: string, text: string, color: string) =>
    setRowStatus(s => ({ ...s, [id]: { text, color } }));

  async function addSelected() {
    const sel = selected;
    if (!sel.length) { toast('Nothing selected — tick at least one camera', 'err'); return; }
    if (!complianceReady(assign)) {
      toast('Select a lawful basis and enter a purpose before adding (DPDP)', 'err'); return;
    }
    setAdding(true);
    let ok = 0;
    for (const d of sel) {
      setStatus(d.id, 'Adding…', 'var(--muted)');
      try {
        const res = await apiFetch<Device>(`/discovery/devices/${d.id}/add`, {
          method: 'POST',
          body: JSON.stringify({
            name: (names[d.id] || '').trim() || null,
            profile_index: bestProfile(d),
            enabled: true,
            recording: assign.recording === 'true',
            zone: assign.zone.trim() || null,
            metadata: wzMeta(assign),
            ...wzCompliance(assign),
          }),
        });
        // The backend leaves a camera disabled when it can't confirm a stream
        // and says why in `error`. Reporting a bare "✓ Added" there sends the
        // operator to the Cameras page to work out why a camera they just
        // added is dark — surface the reason on the row instead.
        if (res.error) setStatus(d.id, '⚠ ' + res.error, 'var(--yellow)');
        else setStatus(d.id, '✓ Added', 'var(--accent2)');
        onAdded((names[d.id] || '').trim() || res.ip || '', res.id);
        ok++;
      } catch (e: any) {
        setStatus(d.id, '✗ ' + e.message, 'var(--red)');
      }
    }
    setAdding(false);
    toast(`${ok} of ${sel.length} camera(s) added to the relay`, ok ? 'ok' : 'err');
    loadDevices();
    refreshCameras();
  }

  const field = (label: string, input: ReactNode) => (
    <div>
      <label style={{ fontSize: 11.5, color: 'var(--muted)' }}>{label}</label>
      {input}
    </div>
  );

  return (
    <div>
      <div className="wz-h">Group, zone &amp; metadata</div>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, maxWidth: 700, marginBottom: 8 }}>
        {field('Zone / group', (
          <>
            <input list="wz-zones" value={assign.zone} onChange={e => setAssign(a => ({ ...a, zone: e.target.value }))}
              placeholder="e.g. Zone A — Entrance" style={{ marginTop: 6 }} />
            <datalist id="wz-zones">
              {zones.map(z => <option key={z} value={z} />)}
            </datalist>
          </>
        ))}
        {field('Camera name prefix', (
          <input value={assign.prefix} onChange={e => applyPrefix(e.target.value)}
            placeholder='optional — e.g. "Gate" → Gate-001' style={{ marginTop: 6 }} />
        ))}
        {field('Lawful basis (DPDP) — required', (
          <select value={assign.lawful} onChange={e => setAssign(a => ({ ...a, lawful: e.target.value }))} style={{ marginTop: 6 }}>
            <option value="">— select a lawful basis —</option>
            {LAWFUL_BASES.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
        ))}
        {field('Purpose (DPDP) — required', (
          <input value={assign.purpose} onChange={e => setAssign(a => ({ ...a, purpose: e.target.value }))}
            placeholder="e.g. Safeguarding staff and stock at the store entrance" style={{ marginTop: 6 }} />
        ))}
        {field('Notice / signage', (
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8, fontSize: 12.5, cursor: 'pointer' }}>
            <input type="checkbox" checked={assign.noticePosted} style={{ width: 'auto' }}
              onChange={e => setAssign(a => ({ ...a, noticePosted: e.target.checked }))} />
            Required notice/signage is posted at this location (DPDP Rule 3)
          </label>
        ))}
        {field('Installation date', (
          <input type="date" value={assign.installed}
            onChange={e => setAssign(a => ({ ...a, installed: e.target.value }))} style={{ marginTop: 6 }} />
        ))}
        {field('GPS coordinates', (
          <>
            <input value={assign.gps} onChange={e => setAssign(a => ({ ...a, gps: e.target.value }))}
              placeholder="optional — 28.6139, 77.2090" style={{ marginTop: 6 }} />
            {assign.gps.trim() !== '' && !parseGps(assign.gps) && (
              <div style={{ fontSize: 11, color: 'var(--yellow)', marginTop: 3 }}>
                Enter as "lat, lng" so cameras can auto-place on a georeferenced map.
              </div>
            )}
          </>
        ))}
        {!!sitemaps.length && field('Place on map', (
          <select value={assign.sitemapId}
            onChange={e => setAssign(a => ({ ...a, sitemapId: e.target.value, mapX: null, mapY: null }))}
            style={{ marginTop: 6 }}>
            <option value="">Not on a map</option>
            {sitemaps.map(s => <option key={s.id} value={String(s.id)}>{s.name}</option>)}
          </select>
        ))}
        {field('Recording (NVR) default', (
          <select value={assign.recording}
            onChange={e => setAssign(a => ({ ...a, recording: e.target.value as 'true' | 'false' }))} style={{ marginTop: 6 }}>
            <option value="true">On — record continuously</option>
            <option value="false">Off — live only</option>
          </select>
        ))}
      </div>
      <div className="d-hint" style={{ marginBottom: 16 }}>
        Zone, lawful basis, GPS and installation date are stamped on every camera added in this batch.
      </div>

      {!!sitemaps.length && assign.sitemapId && (
        <div style={{ marginBottom: 16 }}>
          {selected.length === 1 ? (
            <MapPlacePreview sitemapId={assign.sitemapId} x={assign.mapX} y={assign.mapY}
              onPlace={(x, y) => setAssign(a => ({ ...a, mapX: x, mapY: y }))} />
          ) : (
            <div className="d-hint">
              Cameras will be added to this map unplaced — drag them into position on the Map tab.
            </div>
          )}
        </div>
      )}

      <div className="wz-h" style={{ marginBottom: 8 }}>
        Ready to add{' '}
        <span style={{ color: 'var(--muted)', fontWeight: 500, fontSize: 12 }}>
          {ready.length ? `· ${ready.length} verified` : ''}
        </span>
      </div>
      <div className="wz-rows" style={{ maxHeight: 260 }}>
        {!ready.length && !already.length && (
          <p style={{ color: 'var(--dim)', fontSize: 13 }}>No verified devices yet — assign credentials in step 2.</p>
        )}
        {ready.map(d => {
          const st = rowStatus[d.id];
          const profiles = (d.rtsp_candidates || []).length;
          return (
            <div key={d.id} className="wz-row">
              <input type="checkbox" checked={checks[d.id] !== false}
                onChange={e => setChecks(c => ({ ...c, [d.id]: e.target.checked }))} style={{ width: 'auto' }} />
              <input value={names[d.id] ?? ''} onChange={e => setNames(n => ({ ...n, [d.id]: e.target.value }))}
                style={{ flex: 1, padding: '5px 9px', fontSize: 12.5 }} />
              <span style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--muted)', minWidth: 105 }}>
                {d.ip || '—'}
              </span>
              <span style={{ fontSize: 11, color: 'var(--dim)' }}>
                {profiles} profile{profiles === 1 ? '' : 's'}
              </span>
              <span style={{ fontSize: 11, color: st ? st.color : 'var(--green)' }}>{st ? st.text : '✓ Ready'}</span>
            </div>
          );
        })}
        {already.map(d => (
          <div key={d.id} className="wz-row" style={{ opacity: 0.65 }}>
            <span className="wz-dot" style={{ background: 'var(--accent)' }} />
            <span style={{ fontFamily: 'var(--mono)', fontSize: 11.5, color: 'var(--muted)', minWidth: 105 }}>
              {d.ip || '—'}
            </span>
            <span style={{ fontSize: 12, color: 'var(--text2)', flex: 1 }}>{defaultName(d)}</span>
            <span style={{ fontSize: 11, color: 'var(--accent2)' }}>In relay</span>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 12 }}>
        <button className="btn-primary" disabled={adding || !complianceReady(assign)} onClick={addSelected}
          title={!complianceReady(assign) ? 'Select a lawful basis and enter a purpose first (DPDP)' : undefined}>Add selected cameras →</button>
        <span style={{ color: 'var(--dim)', fontSize: 12 }}>
          Names are editable per camera; the prefix auto-numbers them.
        </span>
      </div>

      <details style={{ marginTop: 18 }}>
        <summary style={{ cursor: 'pointer', fontSize: 12.5, color: 'var(--muted)' }}>
          All discovered devices (advanced — profiles, ignore, per-device add)
        </summary>
        <div style={{ marginTop: 12 }}>
          <DevicesTable devices={devices} loadDevices={loadDevices}
            onSetCreds={setCredDev} onOpenAdd={setAddDev} />
        </div>
      </details>

      <CredsModal device={credDev} onClose={() => setCredDev(null)} onSaved={loadDevices} />
      <AddModal device={addDev} assign={assign} onClose={() => setAddDev(null)}
        onAdded={(name, id) => { onAdded(name, id); loadDevices(); refreshCameras(); }} />
    </div>
  );
}

// ── "Place on map" preview — click the sitemap image to set the single
// selected camera's normalized (0–1) position (legacy sitemap placement). ───

