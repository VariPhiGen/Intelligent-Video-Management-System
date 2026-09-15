# Security Policy

This software records video and holds camera credentials. A vulnerability here
is not an abstraction — it is somebody's building. We would rather hear about a
problem than be the last to know, and this page exists so that a researcher can
decide in under a minute how to tell us.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting.** From the repository, open the
**Security** tab → **Report a vulnerability**. That opens a private advisory
visible only to you and the maintainers, with a place to attach a proof of
concept, a private fork to develop the fix in, and CVE issuance when it is
resolved.

→ https://github.com/VariPhiGen/Intelligent-Video-Management-System/security/advisories/new

If that is unavailable to you — no GitHub account, or you would rather not
create one — email **information@variphi.com** with `SECURITY` in the subject.

**Please do not** open a public issue, pull request, or discussion for a
suspected vulnerability, and please do not post a proof of concept publicly
before a fix exists. A working exploit against an NVR is directly usable against
strangers' cameras.

You do not need to be certain. A report that turns out to be a non-issue costs
us an hour; the one you decided not to send costs somebody their footage.

### What to include

Whatever you have. The reports we can act on fastest tend to have:

- what an attacker gains — read footage, add a camera, recover a stored
  credential, escalate a role, deny recording;
- the position they need to start from (see the trust boundaries below);
- a request sequence, a diff, or a script — enough to reproduce;
- the version: the release tag or commit SHA, and whether the deployment is the
  stock `docker compose` stack or a modified one.

### What to expect

- **Acknowledgement within 3 working days.** If you have not heard from us,
  assume the message went astray and resend, or use the other channel.
- **An initial assessment within 10 working days** — whether we reproduce it,
  our severity read, and a rough fix timeline.
- **Regular updates** until it closes, without you having to chase us.
- **Credit** in the advisory and the release notes under whatever name or handle
  you choose, or none if you prefer. Tell us which.
- **A fix in the open.** The advisory is published when the fix ships, with
  enough detail for operators to tell whether they were exposed. We do not ship
  silent security patches: an operator who cannot tell a security release from a
  routine one cannot prioritise, and quietly fixing it protects us at their
  expense.

We have no bug bounty. We are not going to pretend otherwise to attract reports.

## Scope

**In scope** — this repository and the stack it brings up:

- the API and camera registry (`services/camera-mgmt`)
- the recorder (`services/nvr`) and motion detection (`services/motion`)
- the React SPA (`frontend-react`)
- the shipped deployment: `docker-compose*.yml`, `deploy/`, the `./vms` wrapper,
  and `scripts/` — including default configuration that is insecure out of the
  box, which we treat as a vulnerability in its own right and not a
  documentation gap

**Out of scope**, though still worth telling us about informally:

- vulnerabilities in third-party components (MediaMTX, Keycloak, Postgres,
  Valkey, ffmpeg, or any dependency) — report those upstream; if the exposure is
  caused by *how we configure or pin* the component, that is in scope
- findings that require an attacker to already hold `admin`, or root on the host
- an operator's own deployment choices: exposing the stack directly to the
  internet, reusing the demo credentials, disabling TLS
- `DEV_AUTH=true`, which makes every caller a full administrator by design and
  says so
- missing hardening headers or a TLS grade, absent a demonstrated impact

## Trust boundaries worth attacking

Stated plainly, because a researcher should not have to reverse-engineer our
threat model to know whether a finding matters:

| Boundary | The assumption we are making |
|---|---|
| Unauthenticated network → API | Only login and health are reachable. Everything else requires a valid OIDC token. |
| Authenticated user → other users' data | Roles and capabilities are enforced server-side, per request. The SPA hiding a control is not a security boundary and never was. |
| API → stored camera credentials | Encrypted at rest under `DISCOVERY_SECRET_KEY`. Recovering a plaintext camera password without that key is a vulnerability. |
| Relay → cameras | Each camera is pulled once, by the relay. A consumer of the relay must not be able to reach the camera's own credentials or its ONVIF control surface. |
| Recorded footage | Reachable only through the API's authorisation checks — never by guessing a path, a slug, or a timestamp. |
| Audit log | Append-only from the application's side. An authenticated user, at any role, altering or deleting audit rows through the API is a vulnerability. |

Two things are **deliberately not** boundaries, so please do not report them as
findings: an operator with root on the host can read everything, and anyone who
can reach a camera on its own network can talk to it directly regardless of what
this software does.

## Supported versions

While this project is pre-1.0, security fixes land on the default branch and in
the next release. We do not backport to older tags. If you run a pinned version,
plan to move forward to take a fix.

## Operator checklist

Not part of the intake process, but this is where people look, so:

- **Never run the stack with the shipped example secrets.** It refuses to boot
  with them, which is intentional. `scripts/gen-secrets.sh` generates real ones,
  and `./vms` calls it for you on first run.
- **Rotate with `scripts/rotate-secrets.sh`**, not by hand — rotating
  `DISCOVERY_SECRET_KEY` without re-encrypting orphans every stored camera
  credential, and the script checks for that before it acts.
- **Change the default login** before the appliance is reachable by anyone else.
- **Do not expose the API or Keycloak directly to the internet.** Put them behind
  a VPN or a reverse proxy you control; TLS is available via
  `scripts/gen-certs.sh` and the `tls` compose profile.
- **Keep `.env` at mode 0600.** It holds every secret the stack has.
