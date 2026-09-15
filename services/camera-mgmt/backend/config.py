from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

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

    # ── NVR service (recording; proxied under /api/nvr/*) ─────────────────────
    nvr_api_url: str = Field(
        default="http://127.0.0.1:8009",
        description="Internal base URL of the NVR recording service",
    )
    nvr_default_retention_days: int = Field(default=30)
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

    # ── DeepStream analytics pipeline (same appliance; shared directory) ──────
    # The pipeline consumes one <slug>.json per camera from a directory this
    # API writes (services/deepstream_projection.py) and the pipeline container
    # mounts as its config/camera_config. A directory rather than an HTTP hop
    # because both run on the same air-gapped box: no auth to carry, and the
    # config is on disk before either service starts, so neither has to wait
    # for the other. Set the path to the host directory bind-mounted here.
    #
    # OFF BY DEFAULT: AI activities execute on the CPU Activity Engine in the
    # analytics service (services/analytics/analytics/activities/), fed by the
    # same detector and tracker as Smart Search. DeepStream is kept, unmodified,
    # for deployments that want it back — set DEEPSTREAM_PROJECTION_ENABLED=true
    # and this projection, its push to :8081 and its reconcile loop all return.
    deepstream_projection_enabled: bool = Field(default=False)
    deepstream_config_dir: str = Field(
        default="/deepstream-config",
        description="Directory this API projects per-camera pipeline configs into",
    )
    # The SAME directory as seen from inside the pipeline container. Its
    # add/modify endpoints are handed a path and open it themselves, so they
    # need their own view of the mount, not ours. Keep in step with the
    # pipeline's docker-compose (it mounts its checkout at /apps/…).
    deepstream_pipeline_config_dir: str = Field(
        default="/apps/deepstream-yolo-e2e/config/camera_config",
        description="Same files, as the pipeline container sees them",
    )
    # The pipeline's own REST API (python_module/component/rest_api_server.py).
    # No auth of its own; both containers are host-networked, so loopback.
    deepstream_api_url: str = Field(default="http://127.0.0.1:8081")
    # Only repairs drift (the request-path hooks apply changes immediately), so
    # this is deliberately slow — a sweep restats every file in the directory.
    deepstream_projection_interval: int = Field(
        default=300, description="Drift-repair sweep period (s)"
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

    def local_rtsp_url(self, slug: str) -> str:
        """User-facing relay URL (shown/copied in the UI)."""
        return f"rtsp://{self.server_ip}:{self.rtsp_port}/{slug}"

    def internal_rtsp_url(self, slug: str) -> str:
        """Relay URL as reachable from inside the api service itself."""
        return f"rtsp://{self.relay_internal_host}:{self.rtsp_port}/{slug}"


settings = Settings()
