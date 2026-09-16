# Testing strategy

The test programme for this repository: what is covered, what is deliberately
not, and the standing decisions that should survive the next person who asks
"should we add a coverage gate?"

This document holds **judgement**. The arithmetic lives in
`scripts/coverage_ledger.py` and is spliced in below — see *Regenerating*.
That split is deliberate: the previous version of this ledger was hand-counted
into a published document and was stale by three E2E journeys within a single
afternoon, because a second session kept landing tests while the numbers sat
frozen in prose.

---

## Where it started

**831 tests, ~10,900 lines of test code, and not one line of CI that ran any
of it.** The audit that opened this programme found a strong testing culture
with no automation defending it. Three workflows existed — `cla.yml`,
`notices.yml`, `shared-sync.yml` — and none invoked `pytest` or `vitest`.
`CONTRIBUTING.md` asked contributors to run the suites by hand, which is a
request, not a control: it holds exactly as long as the person remembering it.

So phase zero was not writing tests. It was making the existing ones matter.

---

## The ledger

Generated — do not edit between the markers.

<!-- LEDGER:BEGIN -->
| Service | Src lines | Test lines | Ratio | Tests |
| --- | ---: | ---: | ---: | ---: |
| smartsearch | 8,305 | 4,599 | 0.554 | 382 |
| camera-mgmt | 13,259 | 7,300 | 0.551 | 635 |
| analytics | 6,960 | 3,777 | 0.543 | 350 |
| frames | 1,533 | 664 | 0.433 | 59 |
| nvr | 4,459 | 1,276 | 0.286 | 131 |
| motion | 1,163 | 332 | 0.285 | 19 |
| frontend-react | 17,311 | 2,222 | 0.128 | 217 |
| **total** | **52,990** | **20,170** | **0.381** | **1793** |

Plus 16 repo-level structural tests and 86 E2E assertions across 9 journey specs (J1, J2, J3, J4, J5, J6, J7, J8, J9). **Grand total 1895.**
<!-- LEDGER:END -->

Two things about these numbers, so nobody reconciles them against an older
report and concludes something has regressed:

- **Lines exclude blanks and whole-line comments.** A docstring-heavy module
  should not read as well covered. Earlier hand counts used raw line totals, so
  every absolute figure here is smaller while the *ratios* match.
- **Tests are counted as written, not as collected.** A `@pytest.mark.parametrize`
  case counts once. The number measures intent; pytest's own output measures
  executions, and it will always be the larger of the two.

The ratio is a **shape indicator, not a target**. See the standing decision
against a repo-wide gate below. A service can legitimately move down this table
by having its source written more tightly.

---

## Phases

Done in dependency order, because each unblocked the next.

| Phase | What it bought | State |
| --- | --- | --- |
| **P0** · CI gate | Fixed the one non-hermetic test, wired every suite into a merge gate. Lean-vs-pinned dependency split keeps ML version drift out of pull requests. | done |
| **P1** · Wiring invariants | Structural tests over the authz mount table and the `tracks.py` namespace — the drift class behind eleven sub-track bugs. | done |
| **P2** · HTTP contract | The first in-process HTTP layer this backend has had, driving real RS256 tokens through real authz dependencies. | done |
| **P3** · Frontend capability | jsdom + Testing Library, a fetch-route mock, four representative suites. Found two `r.ok` defects. | done |
| **P4** · Highest-risk backend | smartsearch read path and erasure, camera scoping, the RTSP boundary. | done |
| **P5** · Remaining backend | ONVIF vendor parsing, discovery classification, Keycloak identity, scan orchestration, ingest, the frames broker. | done |
| **E2E** · J1–J9 | Browser journeys against a real fourteen-container stack. | done |

**P0 is the piece that keeps paying.** `.github/workflows/tests.yml` runs eight
matrix legs on every pull request, plus a Tuesday `full-deps` job that lets ML
dependency-range drift go red on its own schedule instead of blocking an
unrelated change.

### E2E journeys

| | Journey | State |
| --- | --- | --- |
| J1 | Keycloak login | green |
| J2 | Authentication failure | green |
| J3 | Camera onboarding | green |
| J4 | Camera status | green |
| J5 | Live view | green |
| J6 | Recording creation | green |
| J7 | Playback with timecode validation | green |
| J8 | Smart Search scope isolation | green |
| J9 | Camera deletion with relay cleanup | green |

All nine green together on 2026-09-11: 88 tests, ~36 minutes, one worker, no
retries. They run nightly from `.github/workflows/e2e.yml`, not on pull
requests.

**J7 reads the timecode rather than looking at it.** The synthetic camera burns
the wall-clock epoch into every frame, and `e2e/fixtures/rtsp-cam/timecode.py`
decodes it by matching glyph bitmaps against a reference the fixture renders for
itself with the same ffmpeg call and the same font. Tesseract was tried first
and rejected on evidence: it read a clean `EPOCH 1789055315` as `01789055312`,
which would have made every playback assertion a coin toss. Asking for a moment
therefore produces an equality — the clip decodes to a run of seconds containing
exactly the second requested — rather than a screenshot somebody has to trust.

**J8 seeds below the boundary and reads above it.** The CLIP index is shared
with the analytics appliance, so the journey writes one row this VMS owns and
one it does not through SmartSearch's own `POST /observations`, then asks the
product. No endpoint offers a way to write a foreign row, and none should — that
is why the seeding is direct and the reading is not. The foreign row's presence
in the index is asserted separately, because otherwise "the product did not show
it" could just mean it was never written.

**J9 is the two-track invariant at the one place reconcile cannot heal it.**
Once a camera row is deleted, nothing in the appliance remembers it ever had a
sub — so a teardown that misses `<slug>_sub` leaves a relay path and an NVR
worker no later pass will collect. The sub track is seeded (`substream.judge`
correctly refuses a sub for the browser-safe H.264 mains the live-view journeys
need), but the relay path is created by the product's own reconcile loop, the
recording is switched on through the operator's endpoint, and the deletion under
test is entirely the product's.

---

## Choosing where a test belongs

The question this section answers is the one every change raises: *I have made a
change — what do I write?* Pick the **cheapest layer that can reliably catch the
thing going wrong**. Cheap means fast, hermetic and specific about what broke.

| What you changed | Write | Why that layer |
| --- | --- | --- |
| A pure function or business rule | **Unit test** | No I/O to arrange, so the failure names the rule rather than the plumbing. |
| Auth, authz, or a security boundary | **Contract/API test**, plus a **structural invariant** if the rule is one a future surface could forget | The contract test proves this endpoint refuses; the invariant proves the *next* endpoint does too. `test_router_authz_wiring.py` is the pattern. |
| Database or search behaviour | **Hermetic integration/API test** | Drive the real query path with the store stubbed. If it needs a live database to pass, it will fail on a runner and teach everyone to ignore it. |
| Behaviour of an external dependency | **Deterministic simulator or stub** | Recorded fixtures (`onvif_fixtures.py`) or a simulator (`e2e/fixtures/onvif-sim`). Never a live third party. |
| A critical cross-service user journey | **E2E** | Only when the claim genuinely spans services and cannot be made lower down. E2E is the most expensive layer in the repository; treat a new journey as a real cost. |
| A load-bearing invariant | **Mutation test** it once | Break it deliberately and confirm a test goes red. A test that has never failed is a claim, not a check. |
| A regression you just fixed | **Focused test at the cheapest layer that catches it** | Where it was *found* and where it should be *guarded* are often different layers — see the note below. |
| Deployment, upgrade, or scale risk | **A separate validation programme**, not ordinary unit coverage | These need an environment, not an assertion. See *Future validation programmes*. |

**Found high, guarded low.** The best worked example in this repository: the
health monitor's orphan sweep deleting live sub-track relay paths was *found* by
mutating an E2E journey, and is *guarded* by five hermetic unit tests plus one
structural invariant. Nothing about the guard needs a container. If you find
yourself writing an E2E test for something a unit test could hold, the finding
came from E2E but the test does not belong there.

**Beware the test that cannot fail.** Two in this programme passed while proving
nothing: a Smart Search isolation suite asserting against an empty feed, and a
deletion assertion that polled long enough for an unrelated safety net to do the
work. Both looked like coverage. When you write an assertion, ask what would
have to break for it to go red — and if the honest answer is "nothing", fix the
test before moving on.

### Rules that apply at every layer

- **No arbitrary sleeps.** Ever. Wait on a state the product reports.
- **Bounded polling** for state that genuinely converges later — with a deadline,
  so a hang becomes a readable failure instead of a timeout.
- **Never poll for something synchronous.** If the product awaits the work
  before responding, assert at the instant of the response. Polling there tests
  whichever backstop is slower, not the operation you meant to test.
- **Deterministic data.** Fixed timestamps, seeded values, no `random`, no
  reliance on wall-clock ordering.
- **Lower layers are hermetic.** No network, no Redis, no database, no device.
  `conftest.py` installs a guard that turns an accidental socket into a failure.
- **External dependencies are isolated** unless the integration itself is the
  subject of the test.

### Rules specific to E2E

- `workers: 1`, `retries: 0`, `fullyParallel: false` — the journeys share one
  appliance, and a retry hides a flake instead of fixing it.
- **One clean appliance**, and never two Playwright processes against it. Doing
  so produces phantom 404s and trace errors that read like product bugs.
- **Product-owned services run real** — api, Postgres, search database, Valkey,
  Keycloak, MediaMTX, NVR, frames/motion, Smart Search.
- **Uncontrollable external systems are synthetic** — RTSP cameras, ONVIF,
  WS-Discovery.
- **No uncontrolled egress.** Nothing in a journey may depend on the internet.
- **DeepStream/GPU is out of scope** by decision, not oversight.

### And the decision that is not a rule

**No blanket coverage gate.** There is no repo-wide percentage threshold and
adding one has been proposed and declined — see *Standing decisions*. The ledger
check in CI asserts only that the documented inventory matches the tree; it sets
no target and will never fail because a ratio moved.

---

## Future validation programmes

Acknowledged risks that are **deliberately not** ordinary test-suite work. Each
needs an environment or a fixture set that does not exist yet, and each would be
its own piece of work with its own decision to start. They are recorded here so
they stay visible instead of being rediscovered.

| Programme | The risk | Why it is not unit coverage | Rough shape |
| --- | --- | --- | --- |
| **Frontend breadth** | Seven of ten page areas have no component test; the ratio is 0.051. A frontend change is materially less safe than a backend one. | Needs deliberate per-page work, not a sweep. The capability exists (Vitest + jsdom + Testing Library); only the breadth is missing. | Pick pages by blast radius — live, cameras, config first. Two known defects now have focused tests; the rest is open. |
| **Upgrade / migration** | Alembic migrations are explicitly untested. For an on-prem product shipped to customers, a bad upgrade is among the worst outcomes and there is no coverage at all. | Needs a real database seeded at an old revision, migrated forward, and asserted — an environment, not an assertion. | Snapshot a populated schema at release N, migrate to N+1, assert data survives and the app boots. |
| **Scale / load / soak** | Nothing exercises many cameras, long-running recording, or memory growth. Reconcile loops, the relay path table and the NVR index are all untested under churn. A VMS runs 24/7. | Findings here are statistical and slow; they do not belong in a per-change gate. | A long-running appliance with synthetic cameras at fleet scale, watching RSS, segment continuity and relay churn. |
| **Real camera interoperability** | The single largest field risk. Every camera in the suite is synthetic; vendor quirks, `rtsps://` with self-signed certificates, and ONVIF dialect differences are inferred rather than observed. | Cannot be made deterministic or put in CI — it needs hardware. | A lab matrix of real devices, replayed into recorded fixtures so findings become hermetic tests afterwards. |
| **ONVIF device control** | `onvif_device.py` (454 lines): imaging, OSD, maintenance. The parsing half is covered; the control half is not. | Control actions change device state, so they need either hardware or a much richer simulator. | Extend the simulator to accept and reflect control calls, then test against it. |

Starting one of these is a decision, not a backlog item. Do not let them turn
into a coverage campaign — the point of listing them is that the risk is *known
and accepted*, which is a different thing from forgotten.

---

## Standing decisions

**Full E2E runs nightly, not per pull request.** `.github/workflows/e2e.yml`
runs J1-J9 on a schedule and on demand. It takes ~36 minutes, needs exclusive
use of host port 8988, and must be a single process against one clean appliance
— none of which belongs on a change-by-change gate. Its concurrency group
(`e2e-appliance`) is global rather than per-branch, because two runs on
different branches would still collide on the compose project name and the
relay's path table. **It has not yet been executed on GitHub Actions** — the
workflow documents the model-download limitation that must be resolved first.

**The ledger is checked in CI, and it is not a coverage gate.**
`scripts/coverage_ledger.py --check` runs in `tests.yml` and fails only when
this document's generated table disagrees with the tree. It sets no threshold
and asserts nothing about the ratios.

**No repository-wide coverage-percentage gate.** The untested mass here is
hardware-facing ONVIF and GPU code. A global threshold pushes effort toward
whatever is cheapest to cover rather than whatever is most expensive to get
wrong. This has been proposed and declined; re-propose it only with an argument
that addresses that specific failure mode.

**A finding becomes a test, not a paragraph.** Every structural gap this
programme found was converted into an invariant that fails CI —
`test_router_authz_wiring.py` *is* the mount-table finding. Prose findings rot;
invariants don't. This is the single most important habit to keep.

**A test that has never failed is a claim, not a check.** 67 mutations were
verified across P0–P5: a deliberate break, confirmed to turn a test red. New
invariant tests should be mutation-checked the same way.

**Never let a job go red for a reason outside the author's change.** This is why
`tests.yml` splits dependency strategy per service rather than being uniform.

### Patterns worth copying

- **Golden-file behaviour pinning.** `golden_motion.json` holds what the
  original implementations produced on 220 deterministic frames, captured
  before replacement; each service that vendors `motion.py` replays it. Catches
  a copy drifting in *behaviour*, not just in bytes. The right model for ONVIF
  vendor quirks and the smartsearch query path.
- **Structural invariants over config.** `test_compose_topology.py` exists
  because a service is added to the base file while the overlay is a separate
  file nobody is forced to open. Any file pair with that property deserves the
  same test.
- **Shallow wiring assertions.** Deliberately not deep: call the endpoint with
  the DB stubbed and assert only which collaborator it reached. A mutation that
  deletes a call has to fail something.

---

## Defects found by writing the tests

Six, all live in the product, none with a reported symptom, all fixed. The four
below were found by writing tests; two more (the health orphan sweep deleting
live sub-track relay paths, and `ready_since` never being filled on the camera
detail endpoint) were found by mutation testing and by an E2E helper
respectively.

1. **A 5xx from `/api/me` mounted the app as administrator** (P3). `fetch` does
   not reject on 5xx, so `.then(r => r.json())` parsed the error body as the
   principal; `me.kind` was undefined and `isAdmin: me.kind !== 'user'`
   evaluated **true**. The API still refused the calls — UI exposure rather
   than privilege escalation, but exactly what the product forbids.
2. **A permanent 401 from Keycloak recursed without bound** (P5). `_req`'s
   comment said "retry once fresh"; nothing made it once. One API call became
   ~1,000 requests and then a `RecursionError`.
3. **A failed auth-config fetch started a default OIDC login** (P3). Same
   missing `r.ok`: a 5xx with a JSON body parsed into a config with no `mode`,
   fell past the dev check, and began a real login against the *default* realm.
4. **A missing crop returned 500 instead of 404** (P4). `FileResponse` does not
   stat at construction, only when Starlette streams it — after the handler
   returns. The `except OSError` was unreachable code.

---

## What is not covered

**Excluded by decision.** DeepStream — `deepstream.py`, `_projection.py`,
`_client.py` (~900 lines) and all GPU pipeline work — as not currently
product-critical. Caddy, the TLS front door, is a deployment shape rather than a
product behaviour.

**Also skipped, permanently:** alembic migrations, research sweep scripts,
generated clients, trivial serialisers.

**Backend still open:**
- ONVIF device and imaging *control* — `onvif_device.py` (454 lines): imaging,
  OSD, maintenance. The parsing half is covered; the control half is not.
- Routers `sitemaps.py`, `peripherals.py`, and parts of `users.py` beyond their
  authz posture.

**Frontend.** Nine of ten page areas have no component test: live, playback
beyond its HLS arithmetic, cameras, config, map, wizard, admin, analytics,
peripherals. P3 bought capability and four representative suites rather than
breadth — that was the intent, not an oversight. The two known open defects
(the video wall's nine-tile ceiling, the cell-crop on non-16:9 cameras) are
unguarded at component level, though the E2E fixture set can now reproduce the
second.

---

## Regenerating

```bash
python scripts/coverage_ledger.py            # print the table
python scripts/coverage_ledger.py --write    # splice it into this file
python scripts/coverage_ledger.py --check    # exit 1 if this file is stale
python scripts/coverage_ledger.py --json     # raw data
```

Run the suites exactly as CI does:

```bash
python -m pytest services/<service>/tests -q
python -m pytest tests -q
npm --prefix frontend-react run test
e2e/scripts/up.sh && cd e2e && npx playwright test
```

When the analysis itself needs revisiting rather than the tests, revise this
document in place. A fresh report alongside it, with no relationship to it, is
how two descriptions of one test suite start to disagree.

---

## Provenance

| | |
| --- | --- |
| Programme opened | 2026-09-10, from the `engineering:testing-strategy` skill (plugin v1.2.0) |
| This document | 2026-09-10, branch `ai` @ b27ac6d; the ledger is regenerated on every change |

The plugin skill defines no output format and writes no files — it says only
"produce a test plan." The structure of this document is ours, which is why it
is checked in here rather than customised inside the versioned plugin cache
(the plugin's versioned cache), where an upgrade would erase it.

The E2E harness decisions — which dependencies run for real, which are
synthetic, and why — are recorded in `e2e/README.md`, next to the code they
describe.
