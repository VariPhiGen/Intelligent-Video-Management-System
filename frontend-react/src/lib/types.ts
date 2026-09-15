/** types.ts — API contracts (mirror the FastAPI response models). */

/** A named region (normalized 0–1 coords): a polygon `zone` or a 2-point
 *  `tripwire` line (with a crossing direction). */
export interface AnalyticsRegion {
  kind: 'zone' | 'tripwire';
  name: string;
  color: string;
  points: number[][];
  direction?: 'both' | 'a2b' | 'b2a';   // tripwire only
}
/** One activity: a catalog type, unique per camera, owning the zones/tripwires
 *  it watches. `params` holds the built-in schedule (`active_hours`,
 *  `active_days`) plus whatever detector fields that type's schema declares. */
export interface AnalyticsActivity {
  type: string;
  regions: string[];
  params: Record<string, any>;
}
/** CMM (Camera Motion Map) analytics config — regions + a list of activities. */
export interface AnalyticsConfig {
  regions: Record<string, AnalyticsRegion>;
  activities: AnalyticsActivity[];
}

/** STQC/BIS-ER posture vocabulary — mirrors the backend's STQC_STATUSES closed
 *  set in models.py. 'unknown' is the default and is never guessed away: an
 *  operator attesting compliance they never verified is worse than no answer. */
export type StqcStatus =
  | 'unknown'
  | 'certified'
  | 'not_certified'
  | 'exempt'
  | 'not_applicable';

/** Display labels for {@link StqcStatus}, in the order the report tallies them. */
export const STQC_LABELS: Record<StqcStatus, string> = {
  certified: 'Certified',
  not_certified: 'Not certified',
  exempt: 'Exempt',
  not_applicable: 'Not applicable',
  unknown: 'Unverified',
};

/** Certificates expiring inside this window are flagged "expiring soon" —
 *  mirrors STQC_EXPIRY_WARN_DAYS in models.py. */
export const STQC_EXPIRY_WARN_DAYS = 90;

export interface Camera {
  id: string;
  name: string;
  slug: string;
  rtsp_url: string;
  local_rtsp_url: string;
  enabled: boolean;
  recording: boolean;
  health_status: 'connected' | 'disconnected' | 'error' | 'unknown' | 'disabled' | string;
  last_seen_at: string | null;
  ready_since: string | null;
  metadata: Record<string, any>;
  privacy_masks: number[][][];
  recording_schedule: { mode: 'weekly' | 'monthly'; rules: any[] } | null;
  motion_detection: boolean;
  /** Whether Smart Search indexes this camera. Default true; an operator can
   *  opt a camera out. Load-bearing for honesty: without it the search page
   *  cannot tell "found nothing" from "never looked here". */
  search_indexing: boolean;
  /** Which domains this camera contributes: person / vehicles / plate.
   *  "vehicles" is plural to match the search contract. */
  search_domains: string[];
  motion_sensitivity: 'low' | 'medium' | 'high' | null;
  /** NVR retention override in days; null = appliance default. */
  retention_days: number | null;
  /** The camera's low-resolution second stream, or null when it has none. */
  sub_track: SubTrack | null;
  /** Why this retention period is set (DPDP purpose-binding); null if unset. */
  retention_justification: string | null;
  /** DPDP lawful basis + purpose + notice/signage flag. */
  lawful_basis: string | null;
  purpose: string | null;
  notice_posted: boolean;
  /** STQC/BIS-ER certification posture (India, mandatory for cameras sold from
   *  1 April 2026). Never inferred — 'unknown' means nobody has attested it. */
  stqc_status: StqcStatus;
  stqc_certificate_no: string | null;
  /** ISO date (YYYY-MM-DD); null when unset or not applicable. */
  stqc_valid_until: string | null;
  stqc_verified_at: string | null;
  stqc_verified_by: string | null;
  /** The appliance default retention, for effective-policy display/warnings. */
  retention_default_days: number;
  /** Days before footage is groomed to keyframe-only; null = appliance default. */
  groom_after_days: number | null;
  /** CMM analytics config (activity→zone mapping + polygon zones); {} = unset. */
  analytics_config: AnalyticsConfig;
  ip: string | null;
  vendor: string | null;
  model: string | null;
  firmware: string | null;
  onvif_capable: boolean;
  onvif_port: number | null;
  created_at: string;
  updated_at: string;
  sensor_id: string | null;
}

export interface UptimeEntry {
  pct: number | null;
  series: (number | null)[];
}
export type UptimeSummary = Record<string, UptimeEntry>;

export interface MotionCamera {
  name: string;
  state: 'CONNECTING' | 'MONITORING' | 'TRIGGERED' | string;
  sensitivity: string;
  triggered_at: string | null;
  last_motion_at: string | null;
}

export interface DiscoveredDevice {
  id: string;
  ip: string | null;
  status: 'discovered' | 'probing' | 'no_onvif' | 'auth_failed' | 'verified' | 'added' | 'ignored' | 'unreachable' | string;
  vendor: string | null;
  model: string | null;
  firmware: string | null;
  open_ports: number[];
  rtsp_candidates: { profile: string; token: string; url_raw: string; verified: boolean | null }[];
  error: string | null;
  name?: string | null;
}

export interface VmsUser {
  id: string;
  username: string;
  email: string | null;
  first_name: string | null;
  last_name: string | null;
  enabled: boolean;
  roles: string[];
  /** The user's single product role (highest-priority when Keycloak stacks). */
  role: string | null;
}

export interface Role { name: string; description: string | null; members: number }

export interface PolicyCapability {
  id: string;
  label: string;
  hint: string;
  ui_only: boolean;
  soon?: boolean;      // page not built yet — the permission only gates its nav item
  preview?: boolean;   // page is built and navigable, but its data is still demo
}
export interface PolicyCatalog {
  capabilities: PolicyCapability[];
  roles: Record<string, Record<string, boolean>>;
  editable_roles: string[];
}

export interface NvrCoverage {
  camera: string;
  earliest: number | string | null;
  latest: number | string | null;
  segment_count: number;
  gaps: { start: number | string; end: number | string }[];
}

/** One row of the append-only, hash-chained audit trail. `integrity` is
 *  recomputed server-side on read: `mismatch` means the row's stored hash no
 *  longer matches its contents (tampering). */
export interface AuditEntry {
  id: number;
  ts: string;
  actor_type: 'user' | 'service' | 'system' | 'unknown' | string;
  actor: string | null;
  action: string;
  target: string | null;
  detail: Record<string, any>;
  source_ip: string | null;
  outcome: 'success' | 'failure' | string;
  integrity: 'verified' | 'mismatch';
}
export interface AuditPage {
  entries: AuditEntry[];
  total: number;
  limit: number;
  offset: number;
}
/** Full-chain integrity result (`GET /audit/verify`). */
export interface AuditVerify {
  ok: boolean;
  count: number;
  first_broken_id: number | null;
}



export interface SystemHealth {
  status: string;
  total_cameras: number;
  enabled_cameras: number;
  connected_streams: number;
  disconnected_streams: number;
  unknown_streams: number;
  mediamtx_reachable: boolean;
  postgres_reachable: boolean;
  redis_reachable: boolean;
}


// ── Sitemaps (Map tab) ────────────────────────────────────────────────────────
export interface SitemapMeta {
  id: number;
  name: string;
  content_type: string;
  created_at: string;
  /** Cameras with coordinates on this plan — the dots you can see. */
  cameras_placed: number;
  /** Every camera assigned to this plan, placed or still in the unplaced
   * tray. Always >= cameras_placed. */
  cameras_assigned: number;
  /** Georeferencing control points ({x,y} normalized 0–1 ↔ {lat,lng}); null
   * when uncalibrated. 2 points → similarity, 3 → affine. Enables GPS
   * auto-placement of cameras on this map. */
  calibration?: { x: number; y: number; lat: number; lng: number }[] | null;
  /** HA peripherals pinned to this plan ({id, x, y}, x/y normalized 0–1);
   * null when none are placed. Edited via PUT /sitemaps/{id}/ha-devices. */
  ha_devices?: { id: string; x: number; y: number }[] | null;
}



/**
 * A camera's low-resolution second stream.
 *
 * Recording it gives playback something cheaper to serve than the main: either
 * H.264 that can be stream-copied (no transcode at all) or a much smaller
 * picture that is cheaper to transcode. `recording_enabled` is the switch —
 * a resolved sub costs nothing until it is turned on.
 */
export interface SubTrack {
  url_raw: string;
  codec: string;
  width: number | null;
  height: number | null;
  fps: number | null;
  /** Measured, not read from metadata — cameras under-report it. */
  bitrate_mbps: number | null;
  /** The main stream's measured bitrate, for an honest comparison. */
  main_bitrate_mbps: number | null;
  /** Days the sub's footage is kept. Null = the appliance default. */
  retention_days: number | null;
  /** What it is ACTUALLY kept for: the value above or the appliance default,
   *  clamped to the main's retention. Computed server-side — the browser
   *  cannot know either the default or the clamp. */
  effective_retention_days: number | null;
  source: 'onvif' | 'derived' | 'manual';
  verified: boolean;
  recording_enabled: boolean;
  probed_at: string;
}
