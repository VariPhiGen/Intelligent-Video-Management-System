# End-to-end journeys

Browser tests against a **real** VMS stack: nine product services, a real
Keycloak, a real MediaMTX, and three simulators standing in for hardware.

```bash
e2e/scripts/up.sh                 # build fixtures, start the stack, wait for it
cd e2e && npx playwright test     # run the journeys
e2e/scripts/down.sh -v            # remove the stack and its volumes
```

First run only: `cd e2e && npm install && npx playwright install chromium`.

While working on one journey, from `e2e/`:

```bash
npx playwright test tests/j7-playback.spec.ts   # one journey
npx playwright test -g "sub track"              # by test name, across journeys
npx playwright test --headed                    # watch the browser do it
npx playwright show-report                      # the HTML report of the last run
```

`package.json` carries the same things as scripts: `npm run up`, `npm run down`,
`npm test`, `npm run test:headed`, `npm run report`.

A subset still needs the whole stack running — the journeys share one appliance
and `support/global-setup.ts` refuses to start against one that is not ready.
Run only one Playwright process at a time against it; see *Conventions*.

---

## What this suite is for

The repository already has well over a thousand unit, contract and component
tests (`pytest` per service, Vitest in `frontend-react`, both gated by
`.github/workflows/tests.yml`). A browser
test that re-checks something they cover is pure cost, so the bar for admitting
a journey is deliberately high:

> It must cross at least one process boundary that no contract test spans, or
> exercise browser behaviour that jsdom cannot.

Every defect found during P0–P5 sorted into two piles. One was logic — cheap to
catch in milliseconds where it was caught. The other was **wiring**: a router
mounted without its auth dependency, a surface that stopped calling
`tracks.py`, an audit write opening its own session past the injected one.
Wiring between *processes* is only observable here.

---

## The dependency ledger

Everything the product owns runs for real. Everything it cannot control — a
camera, a clock, a neural net — is synthesised so the run is deterministic.
The boundary is not "hard to mock", it is "would make a green run mean less".

| Dependency | Treatment | Why |
|---|---|---|
| api, postgres, searchdb, valkey | **real** | The things under test, plus the migrations applied from empty each run. |
| **keycloak** | **real** | Non-negotiable. The redirect, PKCE S256, `responseMode: query` and the hash-router interaction cannot be mocked meaningfully. |
| mediamtx, nvr, frames, motion, smartsearch | **real** | The relay and recording paths are where the sub-track bugs lived. |
| RTSP cameras | **synthetic** | `fixtures/rtsp-cam` — an RTSP **server** with a burned-in timecode. |
| ONVIF device + WS-Discovery | **synthetic** | `fixtures/onvif-sim` — real SOAP, verified against the product's own `onvif-zeep`. |
| DeepStream / GPU, Caddy, egress | **absent** | Out of scope; a test needing egress is testing the wrong thing. |

`COMPOSE_PROFILES=search,frames` yields exactly those nine services —
`analytics` and `caddy` are excluded by profile, not by patching compose.

---

## How the stack is composed

Three overlays, and the order is the meaning:

```
docker-compose.yml            the product, as it ships
docker-compose.bridge.yml     off host networking, onto an isolated bridge
e2e/docker-compose.e2e.yml    the simulators and E2E-only settings
```

**The product's compose file is never edited for testing.** Everything the
harness needs is additive.

### Why bridge networking, not the Linux default

The base file uses `network_mode: host`, which is right for a real appliance:
WS-Discovery is multicast and RTSP wants zero NAT. For a test stack it is wrong
twice over — it would put discovery probes on whatever LAN the developer is
sitting on (so a real camera could appear in a test result), and two stacks on
one machine would collide on every port.

Multicast still works: a container on a user-defined bridge does receive
another container's datagram to `239.255.255.250:3702`. That was verified before
the harness was built, and it is what lets WS-Discovery be exercised for real
inside a network that reaches nothing else.

### Isolation

`--project-name vms-e2e` gives this stack its own containers, network and
volumes. A developer's own stack (default project name) is untouched, and
`down.sh -v` cannot reach it. Ports live in the 19xxx block.

---

## Traps worth knowing before you change anything

These each cost an hour to find. They are documented at their call sites too.

**`RTSP_PORT` is not a host-only port.** It looks like one — the bridge overlay
publishes `"${RTSP_PORT}:8654"` — but the container side is the literal 8654
from `deploy/mediamtx.yml`, and the api builds its internal relay URL from the
same variable. Moving it points the api at a port MediaMTX is not listening on,
and every camera fails to relay with no obvious cause. Host isolation comes from
not publishing MediaMTX's ports at all.

**Compose healthchecks hardcode ports that are otherwise variables.**
`NVR_PORT`, `MOTION_PORT` and `SMARTSEARCH_PORT` bind `${VAR}` while their
healthchecks probe literal 8009/8012/8013. Remap one and the service runs fine
while reporting unhealthy forever. Those three are deliberately left at their
defaults; they are internal-only on the bridge, so there is nothing to avoid.

**Keycloak's quick-login check will lock accounts.** Two login attempts inside
one second lock a user for 60 seconds — **without incrementing `numFailures`**,
so the account reads as perfectly healthy while refusing a correct password.
Bad-password journeys therefore use a throwaway account (`e2e-badlogin`) whose
attack-detection state is cleared after every test. Point failures at a shared
account and the mystery comes straight back, three tests later.

**`vms-web` has direct access grants disabled**, correctly — it is a public
client. There is no password-grant shortcut to a user token; journeys perform a
real login. `admin-cli` on the *master* realm is available for inspecting
Keycloak itself, never for impersonating a product user.

**ffmpeg's `drawtext` fails silently.** Escaping the colon in
`%{localtime:%s}` does not survive the filtergraph parser; only single-quoting
the whole text value works. ffmpeg treats the failure as a **warning and exits
0**, so the stream publishes with a frozen literal timecode and a test asserting
on it would compare against a constant and pass. Check stderr for
`Unterminated`, not the exit status.

**MediaMTX is a pull relay, not a publish target.** The product's MediaMTX only
holds paths the api created, so a fixture that pushes into it fails with
`path '…' is not configured`. A camera is a **server**; the VMS pulls from it.

**The burned-in timecode runs ~2 s behind the NVR's timeline.** The timecode is
the camera's clock at capture; the NVR indexes when the frame *arrived* to be
written, after the encoder, relay and segment writer. A clip requested at T
comes back centred on T-2. A `before=2&after=2` window is narrower than that
offset, so the requested second falls off the end of the clip and the test
reads as a product fault when it is not. J7 uses ±6 for the equality check. The
flip side: J7 cannot detect a cutter skew smaller than that latency.

**Smart Search model weights are not in the image.** They download into the
`models` volume on first start. A developer appliance has that volume warm, so
this is invisible; a fresh machine or CI runner needs outbound network and
several minutes before ingest is ready. See the header of
`.github/workflows/e2e.yml`.

---

## Layout

```
e2e/
  .env.e2e                  ports, fixed non-weak secrets, DEV_AUTH=false
  docker-compose.e2e.yml    simulators + E2E-only service settings
  playwright.config.ts      no retries, one worker, traces on failure
  scripts/up.sh             build, start, and wait on state (never a sleep)
  scripts/down.sh
  fixtures/rtsp-cam/        timecoded RTSP server (16:9 and 4:3)
    timecode.py             reads the timecode back by glyph matching (not OCR)
  fixtures/onvif-sim/       ONVIF SOAP + WS-Discovery responder
  support/
    env.ts                  reads .env.e2e — one source of truth for ports
    auth.ts                 real Keycloak login; no token shortcuts
    api.ts                  service / anonymous clients, Keycloak admin token
    stack.ts                relay paths, camera stop/start (docker exec)
    keycloak.ts             throwaway account, lockout clearing
    reset.ts                per-test appliance reset
    cameras.ts              register synthetic cameras, wait on reported state
    timecode.ts             decode the burned-in clock from recorded MP4s
    smartsearch.ts          seed the shared index, including foreign rows
    global-setup.ts         refuses to run against a stack that is not ready
  tests/
    j1-login.spec.ts          11 · redirect, PKCE, callback, session
    j2-auth-failure.spec.ts   13 · bad credentials, anonymous refusal, logout
    j3-onboarding.spec.ts      9 · discovery, DPDP gate, relay, teardown
    j4-camera-status.spec.ts  10 · healthy camera, camera that goes away
    j5-live-view.spec.ts       8 · right stream in the right tile (16:9, 4:3)
    j6-recording.spec.ts       8 · camera → relay → NVR → indexed segment → clip
    j7-playback.spec.ts       10 · requested moment decodes to that timecode
    j8-search-isolation.spec.ts 8 · foreign index rows never reach the UI
    j9-camera-deletion.spec.ts 11 · both tracks torn down and purged
```

---

## Conventions

**No retries, anywhere.** This product's intermittent failures are more likely
to be real — a reconnect race, a relay path not torn down. A retry turns those
into a green tick. A flake is fixed or deleted within a week.

**No sleeps.** Every wait is on an observable condition. `up.sh` polls each
service's own health and prints what it is waiting for; on timeout it dumps the
stack log.

**One worker, fully sequential.** The journeys share one appliance: relay paths
are global and the scan lock is exclusive across the stack. Partitioning that
would mean testing something other than the product.

**Assert outcomes, not layouts.** No screenshot diffing. Where a user-visible
assertion is possible it is preferred; `stack.ts` exists for the facts that have
no pixels, such as whether deleting a camera also removed its sub track.

Failures leave a trace, a video and a screenshot under `test-results/`; passing
runs leave nothing.

---

## Status

**J1–J9 green: 88 tests, ~36 minutes** on one appliance, `workers: 1`,
`retries: 0`. J7–J9 alone take ~22 minutes — recording and indexing dominate.

Because of that runtime the suite is **not** a pull-request gate.
`.github/workflows/e2e.yml` runs it nightly (02:30 UTC) and on
`workflow_dispatch`, with a global concurrency group so two runs never share
the appliance. Run it locally before a release, or before merging anything that
touches auth, the relay, recording, playback, search scope or camera deletion.

Not yet covered by any journey: dashboard, events, configuration, and admin /
user permissions through the UI (role enforcement is covered at the HTTP layer
only).
