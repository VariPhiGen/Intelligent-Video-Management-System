# Hardening the VMS appliance

What this appliance's defaults actually are, what they cost, and what to change
before it carries real footage. Everything here is checkable against the files
it names — nothing is aspirational.

The shipped posture is deliberately **turnkey on a trusted LAN**: an installer
should be able to `./vms up -d` and reach the console from a browser on the
same network without a certificate, a DNS name, or a manual step. Every item
below is the price of that, and how to pay it down.

---

## 1. Sign-in credentials

`deploy/keycloak/realm-vms.json` seeds the application users a fresh appliance
can be signed into with:

| user | realm roles | seeded |
|---|---|---|
| `admin` | `admin`, `operator`, `dpo` | always |
| `operator` | `operator` | only where the realm file carries it — the open-source tree seeds `admin` alone |

Each ships with its username as a literal password. **They are retired
automatically on the first `up`**: `scripts/gen-secrets.sh` generates
`VMS_ADMIN_PASSWORD` (and `VMS_OPERATOR_PASSWORD`, if `operator` is seeded) into
`.env` and prints them once,
and `deploy/keycloak/sync-client.py` (`sync_seed_passwords`) applies them and
stamps `vms_seed_pw=rotated` on each user so a later `up` never overwrites a
password an operator has since chosen.

**Check it worked.** The sync logs one line per user:

```
keycloak-client-sync: retired the shipped password for 'admin' (now the generated VMS_ADMIN_PASSWORD)
```

If instead you see `WARNING VMS_ADMIN_PASSWORD is unset — 'admin' keeps the
SHIPPED password`, that user is still on `admin`/`admin`. That happens on an
appliance upgraded from an older `.env` which has no such key: add both, or
change the passwords in the Keycloak console. The seeded credentials are also
marked `temporary: true`, so even then the first login forces a change.

Separately, `KEYCLOAK_ADMIN_PASSWORD` is the **Keycloak master console**
password, not an application login. It is generated on first run and only takes
effect on a fresh Keycloak database.

**Adding accounts, and recovering the administrator.** A build without
in-product user administration — the open-source tree — manages accounts in the
Keycloak console: `http://<host>:8085/admin` (`KEYCLOAK_PORT`), signed in with
`KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` from `.env`. Switch to the `vms`
realm, then *Users → Add user*, set a password under *Credentials*, and assign
exactly one product realm role (`supervisor`, `operator`, `viewer` or `dpo`)
under *Role mapping*. The API enforces that role's default permissions
immediately; there is no second place to configure.

The same console is the **break-glass path in every build**, and in the
open-source tree it is the only one: resetting a forgotten `admin` password,
unlocking an `admin` after too many failed sign-ins, or moving the `admin` role
to another account. The product deliberately refuses that last one from inside
— there is exactly one administrator and it cannot demote itself — so keep
`KEYCLOAK_ADMIN_PASSWORD` somewhere you can reach when nobody can sign in.

## 2. Secrets

`scripts/gen-secrets.sh` runs automatically on the first `./vms up -d`.

| key | generated | notes |
|---|---|---|
| `INTERNAL_API_KEY` | every run | machine-to-machine callers |
| `KEYCLOAK_ADMIN_PASSWORD` | every run | applies only to a fresh Keycloak |
| `VMS_ADMIN_PASSWORD` / `VMS_OPERATOR_PASSWORD` | every run (operator only if seeded) | applied once per user (§1) |
| `DISCOVERY_SECRET_KEY` | `--fresh` only | derives the Fernet key for camera credentials at rest |
| `SEARCHDB_PASSWORD` + `SEARCHDB_URL` | `--fresh` only | moved together — the URL carries its own copy |
| `POSTGRES_PASSWORD` | never | needs `ALTER ROLE` + three URLs; see below |

**`DISCOVERY_SECRET_KEY` must never be regenerated on an appliance that has
cameras.** It derives the key that encrypts camera credentials at rest
(`backend/crypto.py`); replacing it makes every stored credential
undecryptable, and the failure is silent — `decrypt()` returns `None`, so the
cameras simply appear to have forgotten their passwords. `gen-secrets.sh
--fresh` checks for stored credentials and refuses rather than guessing. To
rotate it safely, re-encrypting as it goes, use `scripts/rotate-secrets.sh
--rotate`.

**`POSTGRES_PASSWORD` is still the shipped default** unless you change it. It
needs an `ALTER ROLE` plus `DATABASE_URL`, `DISCOVERY_DATABASE_URL` and
`KC_DB_PASSWORD` updated in step. Postgres binds `127.0.0.1` only, so this is a
maintenance-window job rather than an emergency.

## 3. Network exposure

Linux uses **host networking** for the media and LAN services — required for
ONVIF WS-Discovery multicast and zero-NAT RTSP. What that means in practice:

| service | bind | reachable from |
|---|---|---|
| api / SPA | `0.0.0.0:8091` | the LAN — this is the console |
| Keycloak | `0.0.0.0:8085` | the LAN — sign-in |
| MediaMTX RTSP / HLS | `8654` / `8988` | the LAN |
| postgres | `127.0.0.1:5433` | loopback only |
| searchdb | `127.0.0.1:5434` | loopback only |
| redis / valkey | `127.0.0.1:6380` | loopback only |
| nvr, motion, frames, analytics, smartsearch | `127.0.0.1` | loopback only — proxied by the api |

The internal services have **no authentication of their own**; they are safe
only because they bind loopback and the api proxies them behind the single
login. Do not publish them. If you change a `*_BIND_HOST` to `0.0.0.0`, you
have removed the only thing protecting that service.

On macOS / Windows the bridge overlay (`docker-compose.bridge.yml`) moves
everything onto an internal Docker network and publishes only the browser-facing
ports.

## 4. TLS

Off by default, and that is a deliberate trade: this is a LAN appliance reached
by IP, and requiring TLS out of the box would lock an installer out of a machine
they just installed. Plain HTTP means **sign-in credentials and footage cross
the LAN in clear text**. Turn it on for anything beyond a trusted network:

```bash
scripts/gen-certs.sh          # offline: local CA + server cert into deploy/certs
./vms --profile tls up -d caddy
```

`scripts/gen-certs.sh` is openssl-only and needs no internet (suitable for
air-gapped sites). Its SANs cover `localhost`, `127.0.0.1`, and the server's LAN
IP and hostname, so browsers validate once they trust `deploy/certs/ca.crt` —
distribute that to the operator workstations.

Caddy then serves two HTTPS vhosts (`deploy/caddy/Caddyfile`):

- **:443** — the SPA, `/api/*`, and `/hls/*` proxied to MediaMTX, with HSTS,
  `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN` and
  `Referrer-Policy: same-origin`.
- **:8444** — Keycloak (not 8443; the NVR API already owns that port).

ACME is disabled and the admin API is off — nothing phones home.

With TLS in front, pin the issuer so tokens are minted for the public URL:

```
OIDC_ISSUER=https://<SERVER_IP>:8444/realms/vms
OIDC_PUBLIC_URL=https://<SERVER_IP>:8444
```

## 5. Keycloak runs in production mode

`docker-compose.yml` starts Keycloak with `start`, not `start-dev`. The
development profile relaxes hostname handling and enables dev-only caching,
which is not what should authenticate a compliance appliance however well it
happens to work. `KC_HTTP_ENABLED` stays on for the reason in §4.

The `vms-web` client is public and **refuses direct access grants** — the
browser authorisation-code flow with PKCE is the only way to obtain a token, and
tokens are held in memory rather than `localStorage`. A separate confidential
`vms-pwcheck` client exists solely for re-authenticating a signed-in user.

## 6. The audit trail

`audit_log` is append-only and hash-chained, so an edited or removed row breaks
the chain rather than disappearing quietly. It records sign-ins and failed sign-ins, camera creation and configuration
changes, Smart Search queries (with the query text and match counts), and every
clip extraction and generated report. Report downloads are themselves audited.

Nothing in the product deletes from this table. If you need it off the box, the
Audit tab in Administration has a CERT-In export.

## 7. Data protection defaults

- A camera **cannot be registered without a DPDP lawful basis and purpose** —
  the API returns 422, and both the add wizard and the manual add require them.
- Retention defaults to 30 days per camera and is configurable per camera.

## 8. Known gaps

These are real and currently unaddressed. They are listed so a deployment can
decide about them rather than discover them.

- **H.265 cameras do not render in live view.** The stream is relayed natively
  and browsers cannot decode HEVC (`services/nvr/api/server.py` says so in its
  own comment). Recording, playback (transcoded) and the whole AI pipeline are
  unaffected; only the live tile is, and it currently reads "Signal lost",
  which misdiagnoses it.
- **Smart Search embeds on CPU even where a GPU is present**, because the torch
  variant is chosen at image-build time. Build with
  `--build-arg TORCH_VARIANT=cu121` on a GPU appliance.
- **`POSTGRES_PASSWORD` ships at its default** (§2).
- The appliance does not manage OS-level hardening — firewalling, SSH,
  unattended upgrades and disk encryption are the host's business, not this
  stack's.
