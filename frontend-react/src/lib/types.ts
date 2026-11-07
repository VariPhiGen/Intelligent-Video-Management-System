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

/** What a camera's ONVIF PTZ service advertises (backend: services/onvif_ptz).
 *  Axes follow the ONVIF convention throughout: pan +1 = right, tilt +1 = UP,
 *  zoom +1 = tele, all normalised to -1..1. */
export interface PtzCapabilities {
  supported: boolean;
  /** Why not, when `supported` is false — shown verbatim, because "this is a
   *  fixed camera" and "the PTZ service is there but no profile is bound to it"
   *  need different things done about them. */
  reason?: string;
  profile_token?: string;
  node_token?: string | null;
  /** Move kinds the camera advertises. A camera that reports no spaces at all
   *  degrades to continuous-only — see the backend's _spaces(). */
  continuous?: { pan_tilt: boolean; zoom: boolean };
  relative?: { pan_tilt: boolean; zoom: boolean };
  absolute?: { pan_tilt: boolean; zoom: boolean };
  spaces_reported?: boolean;
  presets?: { supported: boolean; max: number | null };
  home?: { supported: boolean };
  aux_commands?: string[];
  probed_at?: string;
  /** Movement limits the camera reports, in its own units (i-PRO pan is 0..1, zoom 0..0.09375). */
  ranges?: { pan?: [number, number]; tilt?: [number, number]; zoom?: [number, number] };
}

/** Backend services/ptz_view_changes: "shifted" runs; "review" / "awaiting_picture" pause the preset's rules. */
export interface PtzZoneReview {
  state: 'shifted' | 'review' | 'awaiting_picture';
  note: string;
  reason: string;
  by: string | null;
  at: string;
  offset: [number, number] | null;
  zoom_changed: boolean;
  can_undo: boolean;
  paused: boolean;
}

/** A deleted preset's zones + rules, kept to be copied onto another preset. */
export interface PtzArchivedView {
  id: string;
  name: string;
  token: string | null;
  ai: { zones: number; tripwires: number; rules: number };
  reason: string | null;
  archived_by: string | null;
  archived_at: string | null;
  has_picture: boolean;
}

export interface PtzPreset {
  /** VMS's id for the preset — stable; the token is only the camera's slot number (reused after a delete). */
  id?: string;
  token: string;
  name: string | null;
  pan: number | null;
  tilt: number | null;
  zoom: number | null;
  // VMS's record of the preset (services/ptz_presets) — absent from older backends.
  is_home?: boolean;
  sort_order?: number;
  missing_on_camera?: boolean;
  has_thumbnail?: boolean;
  thumbnail_at?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  /** Per-preset AI (each preset is a fixed-camera view): what it watches, and its reference pictures. */
  ai?: { zones: number; tripwires: number; rules: number };
  reference_at?: string | null;
  reference_night_at?: string | null;
  /** The view changed under its zones (re-saved / re-stored on the camera): moved with it, or paused for a check. */
  review?: PtzZoneReview | null;
  has_previous_reference?: boolean;
  /** null = this user may not see movement history ("PTZ movement history" permission) */
  visits?: number | null;
  last_visit?: string | null;
}

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
  /** Whether Smart Search indexes this camera. Default FALSE since migration
   *  038 — a newly added camera records and runs no AI until asked. Load-bearing
   *  for honesty: without it the search page cannot tell "found nothing" from
   *  "never looked here". */
  search_indexing: boolean;
  /** Which domains this camera contributes: person / vehicles / plate / face.
   *  "vehicles" is plural to match the search contract. Stored independently of
   *  `search_indexing` and means nothing while that is false, so switching
   *  indexing on is one click that works rather than one that appears not to. */
  search_domains: string[];
  /** Time-lapse switch (migration 057). Settings live behind /timelapse — only
   *  the switch rides on the camera, so the Recording tab can say a camera still
   *  captures stills after recording is turned off. */
  timelapse_enabled?: boolean;
  motion_sensitivity: 'low' | 'medium' | 'high' | null;
  /** NVR retention override in MINUTES; null = appliance default. Minutes
   *  because the recorder writes 60 s segments and deletes whole ones, so a
   *  minute is the finest period it can honour — and a lobby camera kept for
   *  20 minutes is a policy days could not express at all. */
  retention_minutes: number | null;
  /** The same period spelled for humans ("45 minutes" / "30 days"), rendered
   *  server-side so every surface that only DISPLAYS it words it identically
   *  instead of each re-deriving days and disagreeing about rounding. */
  retention_label: string;
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
  /** The appliance default retention in MINUTES, for effective-policy
   *  display and the shorten-retention warning. */
  retention_default_minutes: number;
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
  /** Whether this camera's ONVIF service needs HTTPS — most don't. A minority
   *  of firmwares reject at least one ONVIF call over plain HTTP with a SOAP
   *  TransModeError, which otherwise shows up as PTZ/encoder/imaging/OSD all
   *  502ing while the RTSP stream itself plays fine. */
  onvif_use_https: boolean;
  /** True only when the DEVICE answered "yes, I pan" (ONVIF PTZ service + a
   *  media profile bound to a PTZ configuration). This is the gate for every
   *  PTZ control in the UI: a fixed camera never shows one. */
  ptz_capable: boolean;
  /** The cached capability probe. `null` means NEVER PROBED, which is not the
   *  same as "no PTZ" — the UI must say "not checked yet", not "fixed camera",
   *  when this is null. See services/onvif_ptz.probe. */
  ptz: PtzCapabilities | null;
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
/** One camera view calibrated against one plan.
 *
 *  Placement says where a camera stands; this says what ground it covers. The
 *  homography runs FRAME -> PLAN; projecting a plan-space zone into the frame
 *  uses its inverse. */
export interface CameraRegistration {
  id: number;
  camera_id: string;
  sitemap_id: number;
  /** '' for a camera's fixed view; an ONVIF preset token for one recallable
   *  view of a PTZ. */
  preset_token: string;
  /** 3x3 row-major, frame -> plan. */
  homography: number[][];
  landmark_pairs: { fx: number; fy: number; mx: number; my: number }[];
  /** RMS in normalized plan units — always present, always comparable. */
  reprojection_error: number;
  /** The same error in metres, or null when the plan carries no georeferencing.
   *  Null rather than 0, because 0 would read as a flawless fit on exactly the
   *  plans where accuracy cannot be verified. */
  reprojection_error_m: number | null;
  /** The plan area this view covers, clipped to the plan. Empty when the view
   *  and the plan do not overlap at all. */
  footprint: number[][];
  calibrated_at: string;
  calibrated_by: string | null;
}

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
  /** MINUTES the sub's footage is kept. Null = the appliance default. */
  retention_minutes: number | null;
  /** What it is ACTUALLY kept for, in MINUTES: the value above or the
   *  appliance default, clamped to the main's retention. Computed
   *  server-side — the browser cannot know either the default or the clamp. */
  effective_retention_minutes: number | null;
  source: 'onvif' | 'derived' | 'manual';
  verified: boolean;
  recording_enabled: boolean;
  probed_at: string;
}
