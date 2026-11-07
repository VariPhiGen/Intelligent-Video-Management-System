from __future__ import annotations

from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    events_enabled: bool = True
    events_feed_enabled: bool = False
    events_public_base_url: str = ""
    events_deployment_id: str = "vms"
    events_retention_days: int = Field(default=7, ge=1, le=365)
    events_include_plates: bool = False
    events_router_interval_s: float = Field(default=1, ge=0.1)
    events_sender_interval_s: float = Field(default=1, ge=0.1)
    events_lease_s: int = Field(default=60, ge=45)
    # Durable event snapshots for email/SFTP (event_media). One image is capped
    # at media_max_bytes; past spool_mb in total no new ones are stored, so an
    # outage of the receiver cannot turn into unbounded database growth.
    events_media_max_bytes: int = Field(default=2_000_000, ge=10_000)
    events_media_spool_mb: int = Field(default=512, ge=1)

    # ── Network ──────────────────────────────────────────────────────────────
    server_ip: str = Field(default="127.0.0.1", description="Server IP used in local RTSP URLs")
    rtsp_port: int = Field(default=8654)
    api_port: int = Field(default=8091)

    # ── Persistence ──────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://rtsp:rtsp_secret@127.0.0.1:5432/rtsp_relay"
    )
    redis_url: str = Field(default="redis://127.0.0.1:6380/0")

    # ── MediaMTX ─────────────────────────────────────────────────────────────
    mediamtx_api_url: str = Field(default="http://127.0.0.1:9987")
    # MediaMTX's HLS server (hlsAddress in deploy/mediamtx.yml). The SPA fetches
    # live preview from here directly over HTTP on the LAN, but over HTTPS — and
    # in particular through a single-port reverse proxy / tunnel (cloudflared)
    # that only forwards 443 — it uses the same-origin /hls/* path, which this
    # api reverse-proxies here. Keep the port in step with mediamtx.yml and the
    # SPA's hlsSrc() (frontend-react/src/lib/api.ts).
    mediamtx_hls_url: str = Field(default="http://127.0.0.1:8988")

    # ── Workers ──────────────────────────────────────────────────────────────
    max_transcode_workers: int = Field(default=8)

    # ── Health monitor ───────────────────────────────────────────────────────
    health_poll_interval: int = Field(default=30, description="Seconds between health checks")
    # Auto-remediation for recorders with a broken source clock. A camera whose
    # RTSP path is ready while its HLS playlist will not serve is showing the
    # signature of that fault and nothing else — a genuinely offline camera
    # fails both. Requiring several consecutive polls keeps a muxer that is
    # merely slow to produce its first segment from tripping it.
    hls_probe_enabled: bool = Field(default=True)
    hls_probe_timeout: float = Field(default=8.0, description="Seconds to wait for a playlist")
    hls_fail_threshold: int = Field(default=4, description="Consecutive failures before re-stamping")
    reconnect_delays: list[int] = Field(
        default=[5, 10, 30, 60, 120, 300],
        description="Exponential backoff delay sequence in seconds",
    )
    uptime_events_retention_days: int = Field(
        default=90,
        description="Days of camera status-transition history kept for uptime graphs",
    )
    crash_gap_marks_down: bool = Field(
        default=True,
        description=(
            "After an ungraceful death (power cut / host off / OOM), attribute "
            "the unmonitored gap to cameras that were live as 'disconnected' "
            "(Down) instead of 'unknown' (No data). Set False to keep the whole "
            "gap as No data (excluded from uptime %). Only affects crash "
            "recovery; graceful shutdown/restart gaps stay No data regardless."
        ),
    )

    # ── Identity / OIDC (Keycloak) ───────────────────────────────────────────
    # All user auth is OIDC: the API validates Bearer JWTs against the issuer's
    # JWKS. Keycloak is the production issuer; any OIDC issuer works. See §16.
    oidc_issuer: str = Field(
        default="http://localhost:8085/realms/vms",
        description="OIDC issuer URL; JWKS is derived from it unless overridden",
    )
    oidc_jwks_url: str = Field(
        default="", description="Override JWKS URL (default: <issuer>/protocol/openid-connect/certs)"
    )
    oidc_audience: str = Field(
        default="", description="Expected JWT 'aud'; empty disables audience checks"
    )
    oidc_client_id: str = Field(
        default="vms-web", description="Public client id the SPA logs in with"
    )
    # Browser-facing Keycloak base URL for the SPA adapter. EMPTY (the default)
    # means "derive from the browser's own hostname + KEYCLOAK_PORT" — the
    # turnkey mode that works from localhost and any LAN address alike. Set it
    # only to pin a single URL (e.g. the TLS front door).
    oidc_public_url: str = Field(default="")
    # Per-front-door Keycloak URLs, for deployments reached through more than one
    # origin at once (e.g. a LAN IP AND a Cloudflare tunnel). Format:
    #   app_host=auth_base_url,app_host2=auth_base_url2
    # When a browser loads the SPA from `app_host`, /auth/config hands back the
    # matching `auth_base_url` so the Keycloak adapter talks to a reachable
    # endpoint instead of guessing `<app_host>:KEYCLOAK_PORT` — a port a tunnel
    # does not forward. A host absent from the map falls through to the
    # derive-from-hostname turnkey path, so LAN access is untouched. Example:
    #   OIDC_PUBLIC_HOSTS=office-nvr.vgiskill.com=https://auth-office-nvr.vgiskill.com
    oidc_public_hosts: str = Field(default="")
    keycloak_port: int = Field(
        default=8085, description="Port Keycloak listens on (for derived URLs)"
    )
    # DEV ONLY — bypass OIDC entirely and treat every request as a full admin so
    # the stack boots without Keycloak. MUST be false in production.
    dev_auth: bool = Field(default=False)

    # Allowed CORS origins (comma-separated). Empty = same-origin only (the SPA is
    # served by this app behind the same TLS front door, so that's the norm).
    cors_origins: str = Field(default="")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or (
            self.oidc_issuer.rstrip("/") + "/protocol/openid-connect/certs"
        )

    @property
    def oidc_realm(self) -> str:
        return self.oidc_issuer.rstrip("/").rsplit("/realms/", 1)[-1]

    @property
    def oidc_public_host_map(self) -> dict[str, str]:
        """`{app_host: auth_base_url}` parsed from OIDC_PUBLIC_HOSTS. Malformed
        or empty entries are skipped; the URL is normalised (no trailing slash),
        the host lower-cased and port-stripped so header matching is exact."""
        out: dict[str, str] = {}
        for pair in self.oidc_public_hosts.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            host, _, url = pair.partition("=")
            host = host.strip().lower().split(":")[0]
            url = url.strip().rstrip("/")
            if host and url:
                out[host] = url
        return out

    def oidc_public_url_for(self, host: str | None) -> str | None:
        """Browser-facing Keycloak base URL for the origin the SPA loaded from.

        A mapped front door (OIDC_PUBLIC_HOSTS) wins; otherwise fall back to the
        single OIDC_PUBLIC_URL pin, or None to let the SPA derive the URL from
        its own hostname (the LAN turnkey path)."""
        if host:
            mapped = self.oidc_public_host_map.get(host.lower().split(":")[0])
            if mapped:
                return mapped
        return self.oidc_public_url.rstrip("/") or None

    @property
    def oidc_allowed_issuers(self) -> frozenset[str]:
        """Issuers accepted on incoming tokens.

        Keycloak runs with a dynamic hostname (KC_HOSTNAME_STRICT=false), so the
        token's `iss` reflects whichever host the BROWSER used — localhost on
        the box, the LAN IP from another machine, or a public front door reached
        through a reverse proxy / tunnel. Accept the explicit OIDC_ISSUER, the
        well-known local/LAN forms, and every mapped public front door
        (OIDC_PUBLIC_HOSTS) so LAN and tunnel logins validate side by side.
        """
        realm = self.oidc_realm
        hosts = {"localhost", "127.0.0.1", self.server_ip}
        derived = {f"http://{h}:{self.keycloak_port}/realms/{realm}" for h in hosts if h}
        public = {f"{url}/realms/{realm}" for url in self.oidc_public_host_map.values()}
        return frozenset({self.oidc_issuer.rstrip("/")} | derived | public)

    # ── Automation rules (the rule engine) ────────────────────────────────────
    rules_enabled: bool = Field(
        default=True,
        description=(
            "Boot-time default only. The OPERATIONAL control is "
            "POST /api/rules/disarm, which reaches every worker without a "
            "restart; an env var needing a redeploy is not a kill switch."
        ),
    )
    site_timezone: str = Field(
        default="UTC",
        description=(
            "Timezone a rule's active_hours are read in when the rule names "
            "none. An operator writing 22:00-06:00 means local night."
        ),
    )
    alert_retention_days: int = Field(
        default=365,
        description=(
            "Age at which an alert that is neither resolved nor on legal hold "
            "is swept. Resolved alerts and holds are never swept by age."
        ),
    )
    annotation_ttl_minutes: int = Field(
        default=2880,
        description=(
            "How long a loose annotation's frame is kept. MINUTES, because "
            "migration 042 made minutes the canonical retention unit across "
            "the product and a days knob here would reintroduce exactly the "
            "drift that change removed. An annotation attached to a dataset "
            "is exempt and never expires: it has a named owner and a "
            "deletable parent."
        ),
    )

    # ── The peripherals bridge (P2) ──────────────────────────────────────────
    # EVERY ONE OF THESE DEFAULTS TO EMPTY OR OFF. An unconfigured deployment
    # reports `not_configured`, which is a supported state and a different fact
    # from `offline` — the UI draws them differently and an operator is sent to
    # look at a broker only when there is supposed to be one.
    mqtt_url: str = Field(
        default="", description="Broker URL, e.g. mqtts://broker:8883; empty = none")
    mqtt_username: str = Field(default="")
    mqtt_password: str = Field(default="")
    # Off only for a site with a private CA it has not installed. A broker that
    # can open a barrier is not a place to disable certificate checking lightly.
    mqtt_tls_verify: bool = Field(default=True)
    mqtt_topic_prefix: str = Field(default="vms")
    ha_url: str = Field(default="", description="Home Assistant base URL; empty = none")
    # A long-lived HA token. It does not expire, so it is never logged and
    # never returned — see services/bridge/homeassistant.py.
    ha_token: str = Field(default="")
    webhook_driver_enabled: bool = Field(default=True)
    # How often bindings are re-resolved. HA entity ids move between monthly
    # releases, so a binding that stops resolving has to become visibly
    # degraded rather than silently doing nothing.
    bridge_verify_interval_s: int = Field(default=300)
    # How often the ELECTED subscriber re-asserts its topic set against the
    # bindings table. Short on purpose: this is the delay between an operator
    # binding a device and the wall showing its state, and a wall that stays
    # "unknown" for minutes reads as a broken bridge.
    bridge_topic_sync_s: int = Field(default=15)
    # Beyond this a device reading is history, not status, and the wall shows
    # "unknown" instead. Mirrored in the SPA as STALE_AFTER_MS.
    bridge_state_stale_s: int = Field(default=300)

    # ── NVR service (recording; proxied under /api/nvr/*) ─────────────────────
    nvr_api_url: str = Field(
        default="http://127.0.0.1:8009",
        description="Internal base URL of the NVR recording service",
    )
    nvr_default_retention_days: int = Field(default=30)
    # The appliance retention default in MINUTES, and the canonical spelling —
    # retention is minute-granular now (services/retention_units.py). Left
    # unset, it falls back to nvr_default_retention_days above, so an appliance
    # configured before minutes existed keeps its policy across the upgrade
    # without anyone editing .env. Set NVR_RETENTION_MINUTES to express a period
    # days cannot: a lobby camera kept for 20 minutes, a rig kept for an hour.
    nvr_retention_minutes: int | None = Field(default=None)
    # Days a camera's low-resolution sub track is kept, when the camera does not
    # set its own. Much shorter than the main on purpose: the sub exists so that
    # SCRUBBING recent footage is fast, while the main is what evidence is drawn
    # from and is kept for its full retention. Keeping both the same doubles the
    # storage cost of the feature for no benefit past the first few days —
    # measured on this appliance, 198 GB of sub against 13 GB of actual scrub
    # window. Once the sub ages out, playback of older footage falls back to the
    # main automatically: the NVR checks coverage per track, so a missing sub is
    # simply a track with no footage at that time.
    nvr_sub_retention_days: int = Field(default=3)
    # Same, in minutes, and canonical. Unset = nvr_sub_retention_days above.
    # A main track kept for minutes makes the sub's own default meaningless —
    # tracks.sub_retention_minutes clamps it to the main either way — but a
    # site that wants a 30-minute scrub window over 30-day footage needs this.
    nvr_sub_retention_minutes: int | None = Field(default=None)
    # Host the NVR pulls the relay from. The NVR is co-located with MediaMTX,
    # so loopback is correct and immune to SERVER_IP/network changes; set to a
    # LAN IP only if the NVR ever runs on a different box.
    nvr_record_host: str = Field(default="127.0.0.1")
    # Registry→NVR sync: enabled cameras are pushed into the NVR (recording via
    # the relay URL) on create/update/delete plus a periodic reconcile, because
    # NVR runtime cameras are in-memory and vanish on NVR restart.
    nvr_sync_enabled: bool = Field(default=True)
    nvr_sync_interval: int = Field(default=60, description="Reconcile period (s)")

    # ── MediaMTX relay sync (live-preview / recording source paths) ───────────
    # MediaMTX holds its rtspSource paths in memory only, so any MediaMTX restart
    # (crash, manual stop/start, `compose up` recreate, upgrade) drops every path
    # and all cameras go offline until something re-pushes them. The API adds
    # paths on create/enable and once at its OWN startup, but nothing re-asserts
    # them while a long-lived API keeps running under a MediaMTX that restarts.
    # This loop closes that gap the same way nvr_sync/motion_sync do for their
    # in-memory services: re-add any missing enabled-camera path, drop any left
    # behind by a disabled camera. Presence only — registered-but-disconnected
    # reconnects stay with the health monitor's per-camera backoff.
    mediamtx_sync_enabled: bool = Field(default=True)
    mediamtx_sync_interval: int = Field(default=30, description="Reconcile period (s)")

    # ── Motion service (motion detection; proxied under /api/motion/*) ────────
    # Analyses relay streams for cameras with motion_detection=true. Same
    # trust/lifecycle posture as the NVR: loopback, no auth of its own,
    # in-memory cameras re-asserted by the reconcile loop below.
    motion_api_url: str = Field(
        default="http://127.0.0.1:8012",
        description="Internal base URL of the motion detection service",
    )
    motion_sync_enabled: bool = Field(default=True)

    # ── Analytics service (detection, plates, tracking) ──────────────────────
    # Finds the objects Smart Search records. Same lifecycle posture as the
    # others: in-memory camera set, re-asserted by the reconcile loop below.
    # Empty = not deployed, which is the default (--profile analytics).
    analytics_api_url: str = Field(
        default="",
        description="Base URL of the analytics service; empty = not deployed",
    )
    analytics_sync_enabled: bool = Field(default=True)

    # ── Frame broker (services/frames; broker mode only) ─────────────────────
    # Decodes each camera once and publishes frames + canonical motion for the
    # motion service and Smart Search to consume. Empty = not deployed, which
    # is the default: broker mode is opted into with docker-compose.broker.yml,
    # and that overlay is what sets this URL.
    frames_api_url: str = Field(
        default="",
        description="Base URL of the frame broker; empty = not deployed",
    )
    frames_sync_enabled: bool = Field(default=True)

    # ── Smart Search index service (the WRITE side) ───────────────────────────
    # Tells a local index service which cameras to build itself from. Distinct
    # from smartsearch_api_url below, which is the READ side: a VMS can query a
    # remote index without feeding one, or feed a local one it also queries.
    # Empty = this VMS feeds no index, which is a supported deployment.
    smartsearch_index_url: str = Field(
        default="",
        description="Base URL of the local Smart Search index service; empty = none",
    )
    smartsearch_sync_enabled: bool = Field(default=True)

    # ── CLIP search index (Smart Search; proxied under /api/search/*) ─────────
    # Built by the GPU box from the analytics pipeline's crops. Unlike the NVR
    # and motion services this one is not necessarily co-located — on this
    # deployment it is a separate host reached over the public name — so the
    # default is a URL, not loopback, and the hop may cross a network.
    #
    # The service has no auth of its own. Now that browsers reach it only
    # through this API, its exposure should be narrowed to the appliances that
    # proxy for it (egress allow-list, mTLS, or the shared key below); until
    # then anyone who can route to it can query the index unauthenticated.
    # Empty is a supported state, not a misconfiguration: a deployment without
    # a CLIP index reports Smart Search as unavailable-by-design rather than
    # failing every query with a transport error. See smartsearch_client.
    smartsearch_api_url: str = Field(
        default="",
        description="Base URL of the CLIP index service; empty = not deployed",
    )
    # Sent as `X-API-Key` when set. Empty = no header, matching a service that
    # does not check one yet.
    smartsearch_api_key: str = Field(default="")
    # The Smart Search query planner (provider, model, keys) is configured
    # only in the admin UI and stored encrypted in the database — see
    # services/search_planner/settings.py. No environment overrides.

    # ── DeepStream analytics pipeline (same appliance; HTTP) ─────────────────
    # Cameras are pushed to the pipeline's control API (:8081) as full config
    # envelopes (services/deepstream_client.py): POST /camera/modify|add on
    # create / update / analytics edit, POST /camera/remove on delete, and a
    # periodic full re-push + reconcile. No shared directory: the pipeline
    # writes and owns its own config files. Both are host-networked, so
    # loopback; the request carries X-Internal-Key = INTERNAL_API_KEY, which
    # the pipeline enforces once it is configured with the same key.
    #
    # THE ENGINE SWITCH (services/detection_engine.py). OFF BY DEFAULT: all AI —
    # Smart Search detection (person, vehicle, face, ANPR), playback presence
    # and activities — runs on CPU analytics. TRUE: all of it runs on the
    # DeepStream pipeline, which posts results back to this API, and CPU
    # analytics is handed no cameras at all.
    deepstream_sync_enabled: bool = Field(default=False)
    deepstream_api_url: str = Field(default="http://127.0.0.1:8081")

    # The API root as the PIPELINE calls it back — handed to it in every
    # camera's envelope (services/detection_engine.py). Empty =
    # http://127.0.0.1:<API_PORT> (both host-networked).
    deepstream_callback_url: str = Field(default="")
    # The request-path hooks apply changes immediately; this only repairs
    # drift — and re-adds cameras the pipeline lost while it was down, since a
    # push made during its downtime is lost. Hence shorter than the old 300s.
    deepstream_sync_interval: int = Field(
        default=60, description="Pipeline re-push / reconcile period (s)"
    )
    # How often AI activity events past their expires_at are deleted. The row's
    # deadline is fixed at insert (services/ai_events.py); this only sets how
    # long an expired row may linger — reads already hide it.
    analytics_events_sweep_seconds: int = Field(
        default=3600, description="AI event retention sweep period (s)"
    )


    # ── User management (Keycloak Admin REST API; /api/users/*) ───────────────
    # Bootstrap-admin credentials — the same ones compose provisions Keycloak
    # with. Used to manage users/roles in the product realm from the UI.
    keycloak_admin_url: str = Field(
        default="http://127.0.0.1:8085",
        description="Internal base URL of Keycloak (service name on the bridge)",
    )
    keycloak_admin_user: str = Field(default="admin")
    keycloak_admin_password: str = Field(default="admin")
    keycloak_realm: str = Field(default="vms")

    # ── Camera discovery (native under /api/discovery/*) ─────────────────────
    # Formerly a separate microservice; ONVIF scan/probe now runs in-process
    # against the unified cameras table.
    # Zero-config sweep override: comma-separated CIDRs an AUTO scan (no
    # explicit range) sweeps IN ADDITION to its automatic sources (interface
    # derivation + the subnets of cameras already in the registry). Rarely
    # needed: only for a routed camera VLAN no automatic source can see, or to
    # bootstrap a fresh install inside a macOS Docker/OrbStack VM before the
    # first camera exists. Normally left empty — auto sources cover everything.
    discovery_subnets: str = Field(default="")
    discovery_onvif_ports: list[int] = Field(default=[80, 8000, 8080, 2020, 8899])
    discovery_rtsp_port: int = Field(
        default=554, description="Camera-side RTSP port probed by the network scan"
    )
    discovery_scan_concurrency: int = Field(default=64, description="Max concurrent TCP probes")
    discovery_probe_concurrency: int = Field(default=16, description="Max concurrent ONVIF/RTSP probes")
    discovery_tcp_timeout: float = Field(default=1.0)
    discovery_onvif_timeout: float = Field(default=15.0)
    discovery_rtsp_timeout: float = Field(default=12.0)
    discovery_wsdiscovery_timeout: float = Field(default=3.0)
    discovery_wsdiscovery_probes: int = Field(
        default=3, description="WS-Discovery multicast probes per scan (multicast is "
                               "lossy; extra probes catch cameras that miss the first)"
    )
    # Derives the Fernet key that encrypts stored camera credentials at rest
    # (see crypto.py). Rotating it invalidates stored credentials.
    discovery_secret_key: str = Field(default="dev-insecure-change-me")

    # Shared secret for trusted machine-to-machine callers (e.g. platform
    # integrations). Sent as the X-Internal-Key header; accepted in place of a
    # user token (see security.py).
    internal_api_key: str = Field(default="dev-internal-key-change-me")

    # ── PTZ (ONVIF pan/tilt/zoom) ────────────────────────────────────────────
    # A continuous move is the only camera command that keeps running after the
    # HTTP response returns, so both of these are safety limits, not tuning
    # knobs. ptz_move_timeout is sent to the camera as the ONVIF ContinuousMove
    # Timeout AND used by the server-side watchdog for the cameras that ignore
    # it; the UI re-issues a held direction every ~half that interval.
    ptz_move_timeout: float = Field(
        default=2.0,
        description="Seconds a continuous PTZ move may run before it is stopped",
    )
    # How long one operator keeps exclusive control after their last command.
    # Short on purpose: a PTZ head is a shared physical resource and an operator
    # who walks away must not hold it. Admins bypass the lock.
    ptz_lock_ttl: int = Field(
        default=8, description="Seconds of exclusive PTZ control after the last command"
    )
    ptz_default_speed: float = Field(
        default=0.6, description="Default normalised PTZ speed (0..1) when a caller sends none"
    )

    # ── Audit trail ──────────────────────────────────────────────────────────
    # Failed logins, lockouts and logouts happen inside Keycloak (our API only
    # ever sees valid tokens), so a background loop polls Keycloak's events API
    # and appends them to the audit log. Enabling it also turns on login-event
    # storage in the realm at startup (idempotent). See services/audit.py.
    audit_kc_events_enabled: bool = Field(default=True)
    audit_kc_poll_interval: int = Field(
        default=45, description="Seconds between Keycloak event polls"
    )

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str = Field(default="info")

    # Host the API itself uses to pull frames from the relay (snapshots /
    # thumbnails). Loopback under Linux host networking; the mediamtx service
    # name under the bridge overlay (docker-compose.bridge.yml).
    relay_internal_host: str = Field(default="127.0.0.1")

    # ── Data Collection (migration 045) ──────────────────────────────────────
    # Where sampled training frames land. One directory per dataset beneath it,
    # so deleting a dataset row can delete its bytes in one move. Backed by the
    # `dataset_data` volume, deliberately separate from `evidence_data`: these
    # are deletable working images on an operational clock, not an append-only
    # legal register, and must be removable without touching evidence.
    dataset_dir: str = Field(default="/dataset-frames")

    # In-flight snapshot requests per dataset run. The cost of one extraction is
    # a property of the appliance (segment length, codec, whether NVENC is
    # available), so the safe value is a deployment decision, not a constant.
    # Too high and every request overruns the NVR's ffmpeg budget and the run
    # collects nothing — see services/dataset_build._CONCURRENCY.
    dataset_snapshot_concurrency: int = Field(default=2, ge=1, le=16)

    # Label Studio, the annotation tool datasets are pushed to. BLANK IS A
    # VALID STATE and means "not configured": collection still works, and the
    # export route answers 503 with a message rather than failing obscurely.
    #
    # The URL must be reachable FROM THE API PROCESS, which is not the same as
    # from the operator's browser — http://localhost:8080 names the API's own
    # container and is the most common misconfiguration here.
    labelstudio_url: str = Field(default="")
    # Account & Settings -> Access token. Usually a long-lived REFRESH token,
    # not an access JWT; labelstudio_client probes for which it is.
    labelstudio_api_key: str = Field(default="")
    # Fallback project for imports that do not name one. Blank = the operator
    # enters a project id per import.
    labelstudio_project_id: Optional[int] = Field(default=None)


    # ── S3 mirror ────────────────────────────────────────────────────────────
    # CREDENTIALS ONLY. The destination bucket and prefix are NOT settings:
    # they are chosen per import in the UI, so one appliance can feed several
    # datasets to different destinations without a redeploy. A secret key must
    # never round-trip through a browser; a bucket name is not a secret.
    #
    # Leaving the key pair blank is supported and often correct — boto3 then
    # falls back to an instance role or a mounted ~/.aws.
    aws_access_key_id: str = Field(default="")
    aws_secret_access_key: str = Field(default="")
    aws_region: str = Field(default="")
    # Prefill only. These seed the import dialog's fields; the value actually
    # used is whatever the operator submits with the import.
    aws_s3_bucket: str = Field(default="")
    aws_s3_prefix: str = Field(default="")

    # ── Heatmap object store (MinIO) ─────────────────────────────────────────
    # WHERE THE OVERLAY BYTES LIVE. A heatmap JPEG is ~300 KB and a camera
    # produces 17 keepers a day (16 half-hourly snapshots plus the session's
    # final), so they belong in an object store rather than in Postgres rows.
    # The `live` picture is the exception and deliberately stays in the row:
    # it is one per session, overwritten every 30 s, and never read again after
    # that — round-tripping it through the store would be ~2,880 PUT/DELETE
    # pairs per camera per day for an object nobody keeps.
    #
    # SEPARATE FROM the AWS keys above. That pair is the operator's own S3 for
    # dataset export, chosen per import; this is the appliance's internal store
    # and is never shown to a browser. Blank endpoint = no object store, and
    # heatmaps fall back to storing bytes in the row exactly as before.
    heatmap_s3_endpoint: str = Field(default="")
    heatmap_s3_bucket: str = Field(default="vms-heatmaps")
    heatmap_s3_access_key: str = Field(default="")
    heatmap_s3_secret_key: str = Field(default="")

    # ── Time-lapse (services/timelapse.py) ────────────────────────────────────
    # Per-camera stills on a schedule, rendered into videos. OFF per camera by
    # default (cameras.timelapse_enabled); these are the appliance-wide knobs.
    # Stills share the heatmap object store and bucket under their own
    # `timelapse/` prefix — one store, one credential set, one backup — and fall
    # back to bytes in the row when no store is configured, as heatmaps do.
    timelapse_capture_enabled: bool = Field(
        default=True,
        description="Appliance-wide kill switch for time-lapse capture. Per-camera "
                    "switches still default OFF; this only stops cameras that are on.",
    )
    timelapse_tick_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    # Two decodes in flight at most. Each is one FFmpeg reading the relay for a
    # keyframe (~2-3 s); with the 10 s shared snapshot cache, 2 slots cover
    # dozens of cameras at the default 5-minute interval.
    timelapse_capture_concurrency: int = Field(default=2, ge=1, le=16)
    timelapse_render_retention_days: int = Field(default=30, ge=1, le=3650)
    # Hard ceiling on one render: 20,000 stills at 30 fps is an 11-minute video,
    # already longer than anyone watches a time-lapse for. Keeps a mistaken
    # "render the whole year" from filling the temp disk.
    timelapse_max_render_frames: int = Field(default=20000, ge=100, le=200000)
    # NVENC is tried first only when asked: the api container usually has no GPU,
    # and a failed NVENC attempt costs a whole extra encode before the x264 retry.
    timelapse_nvenc: bool = Field(default=False)
    # The disk the stills live on, for the disk guard (services/timelapse.disk_usage). Compose mounts the MinIO
    # volume here read-only; where it isn't mounted the API's own filesystem is measured instead, which on a
    # standard install is the same Docker disk.
    timelapse_disk_path: str = Field(default="/data/timelapse-disk")

    def insecure_default_secrets(self) -> list[str]:
        """Env-var names of security-critical secrets still at a shipped/insecure
        default. Empty when all are operator-set.

        Enforced at startup (see main.py ``lifespan``) — a production deployment
        (``dev_auth=False``) refuses to boot while this is non-empty, so a missed
        secret can't silently ship a box where a guessed ``X-Internal-Key`` is
        full admin (incl. audit-log forgery), stored camera credentials are
        Fernet-encrypted under a public key, or Keycloak admin is ``admin``.

        Covers both the code defaults above and the placeholders in
        ``.env.example``: any value carrying a placeholder marker, plus the
        known-weak Keycloak values, counts as insecure. Matched case-insensitively.
        The offending *values* are never returned or logged — only the names."""
        markers = ("change-me", "change-for-production", "changeme", "insecure")
        checks = {
            "INTERNAL_API_KEY": (self.internal_api_key, set()),
            "DISCOVERY_SECRET_KEY": (self.discovery_secret_key, set()),
            "KEYCLOAK_ADMIN_PASSWORD": (
                self.keycloak_admin_password, {"admin", "password", "keycloak"}
            ),
        }
        offenders: list[str] = []
        for name, (value, known_weak) in checks.items():
            v = (value or "").strip()
            low = v.lower()
            if not v or low in known_weak or any(m in low for m in markers):
                offenders.append(name)
        return offenders

    @field_validator("nvr_retention_minutes", "nvr_sub_retention_minutes",
                     "labelstudio_project_id",
                     mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """An empty string means "not set", not "invalid".

        docker-compose passes every declared variable through, so an appliance
        that has not set NVR_RETENTION_MINUTES hands this field ``""`` rather
        than nothing at all. Without this the api container refuses to start on
        a stock .env — the exact upgrade every existing appliance performs.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    def local_rtsp_url(self, slug: str) -> str:
        """User-facing relay URL (shown/copied in the UI)."""
        return f"rtsp://{self.server_ip}:{self.rtsp_port}/{slug}"

    def internal_rtsp_url(self, slug: str) -> str:
        """Relay URL as reachable from inside the api service itself."""
        return f"rtsp://{self.relay_internal_host}:{self.rtsp_port}/{slug}"


settings = Settings()
