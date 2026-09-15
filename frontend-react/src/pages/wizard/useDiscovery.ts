/**
 * useDiscovery.ts — discovery-session state for the Add-cameras wizard:
 * the device list, the background scan job (polled every 2s, ported from
 * dscPollScan), and the shared helpers (dscIsCamera, _wzDefaultName,
 * _wzBestProfile, wzMeta, DSC_META) from the legacy SPA.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '@/lib/api';
import type { DiscoveredDevice } from '@/lib/types';

/** DeviceOut also carries credential info the shared type doesn't declare. */
export interface Device extends DiscoveredDevice {
  has_credentials?: boolean;
  username?: string | null;
}

/** A camera added to the relay during this wizard session. */
export interface AddedCam { name: string; id: string }

export type Method = 'onvif' | 'manual' | 'csv' | 'range';

/** Batch fields from the Assign step, stamped on every camera added. */
export interface AssignState {
  zone: string;
  prefix: string;
  lawful: string;
  purpose: string;
  noticePosted: boolean;
  installed: string;
  gps: string;
  recording: 'true' | 'false';
  /** Sitemap chosen in the "Place on map" block; '' = not placed on a map. */
  sitemapId: string;
  /** Normalized (0–1) placement on the chosen sitemap; null until clicked. */
  mapX: number | null;
  mapY: number | null;
}

export const EMPTY_ASSIGN: AssignState = {
  zone: '', prefix: '', lawful: '', purpose: '', noticePosted: false,
  installed: '', gps: '', recording: 'true',
  sitemapId: '', mapX: null, mapY: null,
};

/** DPDP lawful-basis options — s.7 legitimate uses plus consent (must match the
 *  backend's LAWFUL_BASES closed set in models.py). */
export const LAWFUL_BASES = [
  'Consent',
  'Employment purposes',
  'Legal obligation / statutory duty',
  'Public safety / State function',
  'Medical emergency',
  'Court order / legal proceedings',
];

/** Free-form onboarding metadata (gps / installed_at only). Lawful basis,
 *  purpose and notice are now first-class fields — see wzCompliance. */
export function wzMeta(a: AssignState): Record<string, any> {
  const m: Record<string, any> = {};
  if (a.gps.trim()) m.gps = a.gps.trim();
  if (a.installed.trim()) m.installed_at = a.installed.trim();
  if (a.sitemapId) {
    m.sitemap = {
      id: Number(a.sitemapId),
      ...(a.mapX != null && a.mapY != null ? { x: a.mapX, y: a.mapY } : {}),
    };
  }
  return m;
}

/** DPDP purpose-binding fields sent first-class on the add/manual body. The
 *  backend rejects registration (422) without a valid lawful_basis + purpose. */
export function wzCompliance(a: AssignState) {
  return {
    lawful_basis: a.lawful.trim(),
    purpose: a.purpose.trim(),
    notice_posted: a.noticePosted,
  };
}

/** True when the compliance gate is satisfied for this batch (a lawful basis
 *  and a purpose). Used to require them before adding. */
export function complianceReady(a: AssignState): boolean {
  return !!a.lawful.trim() && !!a.purpose.trim();
}

/** status → [badge class, dot color, label] (legacy DSC_META). */
export const DSC_META: Record<string, [string, string, string]> = {
  verified:    ['badge-green',  'var(--green)',   'Ready'],
  auth_failed: ['badge-red',    'var(--red)',     'Wrong password'],
  discovered:  ['badge-cyan',   '#06b6d4',        'Needs creds'],
  no_onvif:    ['badge-yellow', 'var(--yellow)',  'No ONVIF'],
  added:       ['badge-blue',   'var(--accent2)', 'In relay'],
  ignored:     ['badge-gray',   'var(--muted)',   'Ignored'],
  unreachable: ['badge-gray',   'var(--muted)',   'Unreachable'],
  probing:     ['badge-yellow', 'var(--yellow)',  'Probing'],
};

/**
 * A scan sweep answers from routers/NAS/web UIs too. Only devices with camera
 * evidence are shown by default: verified/added/probing, stream profiles
 * found, ONVIF identified the vendor, or an RTSP port is open.
 */
export function dscIsCamera(d: Device): boolean {
  if (['verified', 'added', 'probing'].includes(d.status)) return true;
  if (d.rtsp_candidates && d.rtsp_candidates.length) return true;
  if (d.vendor) return true;
  if ((d.open_ports || []).some(p => p === 554 || p === 8554)) return true;
  return false;
}

/** Legacy _wzDefaultName — "Vendor Model 192.168.2.50". */
export function defaultName(d: Device): string {
  return `${(d.vendor || 'Camera')} ${d.model || ''}`.trim() + ` ${d.ip}`;
}

/** Legacy _wzBestProfile — first verified RTSP candidate, else index 0. */
export function bestProfile(d: Device): number {
  const i = (d.rtsp_candidates || []).findIndex(c => c.verified === true);
  return i >= 0 ? i : 0;
}

export function numberedName(prefix: string, i: number): string {
  return `${prefix}-${String(i + 1).padStart(3, '0')}`;
}

export type ScanState = 'idle' | 'run' | 'done' | 'error';

export interface ScanJob {
  status: string;
  id?: string;
  cidr?: string;
  auto?: boolean;   // zero-config WS-Discovery scan (no IP range)
  phase?: string | null;
  scanned?: number;
  total?: number;
  error?: string | null;
}

export interface Discovery {
  devices: Device[];
  loadDevices: () => Promise<void>;
  scanState: ScanState;
  scanJob: ScanJob | null;
  /** POST /discovery/scan (credential-less — creds are step 2). Omit `cidr` for
   *  zero-config WS-Discovery (no IP range). Throws on error. */
  startScan: (cidr?: string) => Promise<void>;
  /** "Scan again" — back to the idle panel without forgetting the finished job. */
  scanAgain: () => void;
}

export function useDiscovery(onError?: (msg: string) => void): Discovery {
  const [devices, setDevices] = useState<Device[]>([]);
  const [scanState, setScanState] = useState<ScanState>('idle');
  const [scanJob, setScanJob] = useState<ScanJob | null>(null);
  const alive = useRef(true);
  const loadedFor = useRef('');   // job key we already reloaded devices for
  const dismissed = useRef('');   // job key the user cleared with "Scan again"
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const loadDevices = useCallback(async () => {
    try {
      const d = await apiFetch<Device[]>('/discovery/devices');
      if (alive.current) setDevices(d);
    } catch { /* transient — next poll retries */ }
  }, []);

  const pollScan = useCallback(async () => {
    let job: ScanJob | null;
    try { job = await apiFetch<ScanJob>('/discovery/scan'); } catch { return; }
    if (!alive.current) return;
    setScanJob(job);
    if (!job || job.status === 'idle') { setScanState('idle'); return; }
    if (job.status === 'running') { setScanState('run'); return; }
    if (job.status === 'error') { setScanState(dismissed.current === `${job.id ?? ''}error` ? 'idle' : 'error'); return; }
    // done — reload the device list once per finished job, but NOT for a job
    // that's been dismissed (a stale scan from a previous session). That's what
    // keeps a fresh open clean instead of resurfacing the last scan's devices.
    const key = `${job.id ?? ''}${job.status}`;
    if (dismissed.current !== key && loadedFor.current !== key) {
      loadedFor.current = key;
      await loadDevices();
    }
    setScanState(dismissed.current === key ? 'idle' : 'done');
  }, [loadDevices]);

  useEffect(() => {
    alive.current = true;
    // A scan that finished before this session opened is stale — mark it
    // dismissed so its devices aren't reloaded and the wizard opens clean.
    // (No eager loadDevices() here on purpose: devices appear only after a
    // scan run in THIS session, or a manual add.)
    apiFetch<ScanJob>('/discovery/scan')
      .then(job => {
        if (alive.current && job && (job.status === 'done' || job.status === 'error')) {
          dismissed.current = `${job.id ?? ''}${job.status}`;
        }
      })
      .catch(() => { /* no job / transient */ })
      .finally(() => { if (alive.current) pollScan(); });
    const t = setInterval(pollScan, 2000);
    return () => { alive.current = false; clearInterval(t); };
  }, [loadDevices, pollScan]);

  const startScan = useCallback(async (cidr?: string) => {
    // Omit `cidr` → zero-config WS-Discovery (no IP range); the api treats a
    // missing/empty cidr as auto mode.
    await apiFetch('/discovery/scan', {
      method: 'POST',
      body: JSON.stringify({ cidr: cidr ?? null, username: '', password: '' }),
    });
    dismissed.current = '';
    loadedFor.current = '';   // let the fresh job's results load
    setScanState('run');
    pollScan();
  }, [pollScan]);

  const scanAgain = useCallback(() => {
    dismissed.current = loadedFor.current;
    setScanState('idle');
  }, []);

  return { devices, loadDevices, scanState, scanJob, startScan, scanAgain };
}
