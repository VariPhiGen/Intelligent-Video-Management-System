/**
 * StepDiscover.tsx — wizard step 1: method cards (ONVIF / manual / CSV /
 * range) and the credential-less scan panel (idle → run → done / error).
 * Ports wzMethod, dscStartScan, wzScanUI/wzScanSummary/wzScanAgain and
 * dscAddManual from the legacy SPA.
 */
import { useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { apiFetch } from '@/lib/api';
import { useToast } from '@/components/Toast';
import { CsvImport } from './CsvImport';
import { parseGps } from '../map/mapHelpers';
import {
  LAWFUL_BASES, complianceReady, dscIsCamera, wzCompliance, wzMeta,
  type AssignState, type Device, type Discovery, type Method,
} from './useDiscovery';

export function StepDiscover({ method, setMethod, cidr, setCidr, disc, assign, setAssign, zones, onAdded, refreshCameras, onDirectImport, onProceed }: {
  method: Method;
  setMethod: (m: Method) => void;
  cidr: string;
  setCidr: (v: string) => void;
  disc: Discovery;
  assign: AssignState;
  setAssign: Dispatch<SetStateAction<AssignState>>;
  zones: string[];
  onAdded: (name: string, id: string) => void;
  refreshCameras: () => void;
  /** CSV bulk import registered these slugs directly — resolve to session cams. */
  onDirectImport: (slugs: string[]) => void;
  /** Advance the wizard — called after a direct manual add registers a camera. */
  onProceed: () => void;
}) {
  const toast = useToast();
  const [manual, setManual] = useState('');
  const [manualName, setManualName] = useState('');
  const [manualBusy, setManualBusy] = useState(false);

  const { scanState, scanJob } = disc;
  const cams = disc.devices.filter(dscIsCamera);
  const errs = cams.filter(d => d.status === 'auth_failed').length;
  // Registered cameras are listed as a rescan dedupe hint — they aren't new
  // finds, so counting them as "found" makes a scan look more fruitful than it
  // was (a re-scan of an existing camera would read "1 camera found").
  const already = cams.filter(d => d.status === 'added').length;
  const pct = scanJob?.total ? Math.round(100 * (scanJob.scanned || 0) / scanJob.total) : 0;

  async function startScan() {
    // ONVIF = zero-config WS-Discovery (no IP range). IP-range = CIDR sweep.
    const range = method === 'range';
    if (range && !cidr.trim()) { toast('Enter an IP range (CIDR) to scan', 'err'); return; }
    try { await disc.startScan(range ? cidr.trim() : undefined); }
    catch (e: any) { toast(e.message, 'err'); }
  }

  // Manual entry accepts either an rtsp:// URL (verified + added directly) or
  // a bare IP (targeted /32 discovery — credentials follow in step 2).
  async function addManual() {
    const v = manual.trim();
    if (!v) { toast('Enter an IP address or an rtsp:// URL', 'err'); return; }
    setManualBusy(true);
    try {
      if (/^rtsps?:\/\//i.test(v)) {
        // Direct rtsp:// add registers immediately — enforce DPDP purpose-binding
        // up front (a bare IP goes to staging and is gated later in Assign).
        if (!complianceReady(assign)) {
          toast('Select a lawful basis and enter a purpose before adding (DPDP)', 'err');
          setManualBusy(false); return;
        }
        const d = await apiFetch<Device>('/discovery/devices/manual', {
          method: 'POST',
          body: JSON.stringify({
            rtsp_url: v, name: manualName.trim() || null, username: null, password: null,
            enabled: true, recording: assign.recording !== 'false',
            zone: assign.zone.trim() || null, metadata: wzMeta(assign),
            ...wzCompliance(assign), verify: true,
          }),
        });
        toast(`${d.name || d.ip} added to relay${d.error ? ' — ' + d.error : ''}`, 'ok');
        onAdded(d.name || manualName.trim() || d.ip || '', d.id);
        setManual('');
        setManualName('');
        disc.loadDevices();
        refreshCameras();
        // Camera is registered — a manual RTSP add needs no credentials/assign
        // step, so move straight on. Back returns here to add another.
        onProceed();
      } else if (/^\d{1,3}(\.\d{1,3}){3}$/.test(v)) {
        await disc.startScan(v + '/32');  // targeted discovery; creds in step 2
        setMethod('onvif');               // show the scan progress panel
        toast(`Probing ${v}…`, 'ok');
      } else {
        toast('Enter an IPv4 address (e.g. 192.168.2.50) or an rtsp:// URL', 'err');
      }
    } catch (e: any) { toast(e.message, 'err'); }
    finally { setManualBusy(false); }
  }

  const methods: [Method, string, string][] = [
    ['onvif', '⬡ ONVIF auto-discovery', 'Zero-config — cameras announce themselves, no IP range needed. Recommended.'],
    ['manual', '⊕ Manual IP / RTSP', 'Add a single camera by IP address or RTSP URL.'],
    ['csv', '⤓ CSV bulk import', 'Upload a spreadsheet with credentials.'],
    ['range', '⇌ IP range scan', 'Sweep a specific subnet (CIDR) for cameras.'],
  ];

  const mField = (label: string, input: ReactNode) => (
    <div>
      <label style={{ fontSize: 11.5, color: 'var(--muted)' }}>{label}</label>
      {input}
    </div>
  );

  return (
    <div>
      <div className="wz-h">How would you like to find cameras?</div>
      <div className="wz-methods">
        {methods.map(([m, title, sub]) => (
          <div key={m} className={`wz-method${method === m ? ' active' : ''}`} onClick={() => setMethod(m)}>
            <b>{title}</b>
            <span>{sub}</span>
          </div>
        ))}
      </div>

      {/* ONVIF / IP range: scan action + live progress */}
      {(method === 'onvif' || method === 'range') && (
        <div>
          {method === 'range' && (
            <div style={{ maxWidth: 360, marginBottom: 14 }}>
              <label style={{ fontSize: 12, color: 'var(--muted)' }}>IP range (CIDR)</label>
              <input value={cidr} onChange={e => setCidr(e.target.value)} placeholder="192.168.2.0/24"
                style={{ marginTop: 6 }} autoFocus />
            </div>
          )}
          {scanState === 'idle' && (
            <div>
              <button className="btn-primary" style={{ padding: '11px 24px' }} onClick={startScan}>
                {method === 'onvif' ? 'Start auto-discovery →' : 'Start scanning →'}
              </button>
              <div className="d-hint" style={{ marginTop: 10 }}>
                {method === 'onvif'
                  ? 'Sends a WS-Discovery probe and also sweeps this appliance’s own subnet, so cameras are found whether or not they announce themselves — no IP range needed. Credentials are collected in the next step.'
                  : 'WS-Discovery plus a TCP sweep of the range. ONVIF credentials will be collected in the next step.'}
              </div>
            </div>
          )}
          {scanState === 'run' && (
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 11, marginBottom: 10 }}>
                <span style={{ fontSize: 13, color: 'var(--text2)', display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span className="wz-blip" />
                  <span>
                    {scanJob?.auto ? 'Listening for ONVIF cameras' : (scanJob?.phase || 'Scanning network')}…
                    {' '}({scanJob?.scanned ?? 0}/{scanJob?.total || '?'}{scanJob?.auto ? '' : ` on ${scanJob?.cidr || ''}`})
                  </span>
                </span>
                <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--muted)' }}>{pct}%</span>
              </div>
              <div className="wz-bar"><div className="wz-fill" style={{ width: `${pct}%` }} /></div>
            </div>
          )}
          {scanState === 'done' && (
            <div className="wz-okbox">
              <span style={{ fontSize: 18, color: 'var(--green)' }}>✓</span>
              <div>
                <div style={{ fontSize: 13, color: 'var(--text)' }}>
                  {cams.length - already} new camera{cams.length - already === 1 ? '' : 's'} found
                  {already > 0 && ` · ${already} already added`}
                  {' · '}{errs} with credential errors
                </div>
                <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 3 }}>Click Continue to assign credentials</div>
              </div>
              <button className="btn-ghost btn-sm" style={{ marginLeft: 'auto' }} onClick={disc.scanAgain}>Scan again</button>
            </div>
          )}
          {scanState === 'error' && (
            <div style={{ color: 'var(--red)', fontSize: 13 }}>Scan error: {scanJob?.error || 'unknown'}</div>
          )}
        </div>
      )}

      {/* Manual: IP (targeted discovery) or rtsp:// URL (direct add). An rtsp://
          URL is added straight away, so name + metadata are collected here —
          there's no Assign step for it. A bare IP runs ONVIF discovery, where
          the same metadata carries into the Assign step. */}
      {method === 'manual' && (
        <div style={{ maxWidth: 720 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 12 }}>
            {mField('Camera IP or rtsp:// URL', (
              <input value={manual} onChange={e => setManual(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') addManual(); }}
                placeholder="192.168.2.50  or  rtsp://192.168.2.50:554/stream1" style={{ marginTop: 6 }} autoFocus />
            ))}
            {mField('Camera name', (
              <input value={manualName} onChange={e => setManualName(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') addManual(); }}
                placeholder="optional — auto-named if left blank" style={{ marginTop: 6 }} />
            ))}
            {mField('Zone / group', (
              <>
                <input list="wz-zones-m" value={assign.zone}
                  onChange={e => setAssign(a => ({ ...a, zone: e.target.value }))}
                  placeholder="e.g. Zone A — Entrance" style={{ marginTop: 6 }} />
                <datalist id="wz-zones-m">{zones.map(z => <option key={z} value={z} />)}</datalist>
              </>
            ))}
            {mField('Lawful basis (DPDP) — required', (
              <select value={assign.lawful} onChange={e => setAssign(a => ({ ...a, lawful: e.target.value }))} style={{ marginTop: 6 }}>
                <option value="">— select a lawful basis —</option>
                {LAWFUL_BASES.map(b => <option key={b} value={b}>{b}</option>)}
              </select>
            ))}
            {mField('Purpose (DPDP) — required', (
              <input value={assign.purpose} onChange={e => setAssign(a => ({ ...a, purpose: e.target.value }))}
                placeholder="Why this camera records" style={{ marginTop: 6 }} />
            ))}
            {mField('Installation date', (
              <input type="date" value={assign.installed}
                onChange={e => setAssign(a => ({ ...a, installed: e.target.value }))} style={{ marginTop: 6 }} />
            ))}
            {mField('GPS coordinates', (
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
            {mField('Recording (NVR) default', (
              <select value={assign.recording}
                onChange={e => setAssign(a => ({ ...a, recording: e.target.value as 'true' | 'false' }))} style={{ marginTop: 6 }}>
                <option value="true">On — record continuously</option>
                <option value="false">Off — live only</option>
              </select>
            ))}
          </div>
          <button className="btn-primary" disabled={manualBusy} onClick={addManual}>
            {manualBusy ? 'Adding…' : 'Add camera →'}
          </button>
          <div className="d-hint" style={{ marginTop: 10 }}>
            An <b>rtsp:// URL</b> is verified and added directly with the details above (credentials may be inline).
            A bare <b>IP</b> runs a targeted ONVIF discovery instead — credentials are collected next, and this
            metadata carries into the assign step. Ensure the camera is on the same network.
          </div>
        </div>
      )}

      {/* CSV bulk import */}
      {method === 'csv' && <CsvImport onImported={onDirectImport} />}
    </div>
  );
}
