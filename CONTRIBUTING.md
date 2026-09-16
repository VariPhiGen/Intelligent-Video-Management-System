# Contributing

Thanks for considering it. This document tells you how to get a working setup,
what a good change looks like here, and the two legal facts to know before your
first pull request. It is short on ceremony because the codebase carries most
of its own rules — the fastest way to write code that fits is to read the file
you are about to change.

## The two legal facts

1. **The project is AGPL-3.0** (see `LICENSE` and `NOTICE`). Your contribution
   will be distributed under it.
2. **Your first pull request needs a signed CLA.** A bot will ask on the PR;
   signing is one comment. Read `CLA.md` first — it is short, and it explains
   *why* it exists (the project is also offered under a commercial licence, and
   shipping your code there needs your permission). Contributing on company
   time? Your employer likely needs the Corporate CLA instead — see the note at
   the bottom of `CLA.md`.

Security problems are the one thing that must NOT arrive as an issue or PR —
see `SECURITY.md` for the private channel.

## Getting a working stack

The whole product runs from one command:

```bash
cp .env.example .env    # first run generates real secrets via ./vms
./vms up -d             # UI at http://localhost:8091 — user 'admin', password in .env (VMS_ADMIN_PASSWORD)
```

`./vms` is a thin `docker compose` wrapper that picks host/bridge networking
and the GPU overlay for your machine; plain `docker compose up -d` also works
on native Linux. You do not need cameras to develop — the UI, API, and most
flows work against an empty registry, and any RTSP source (e.g. an IP camera
app on a phone, or `ffmpeg -re -stream_loop -1 -i file.mp4 -f rtsp …`) can
stand in for real hardware.

### Frontend loop

```bash
cd frontend-react
npm ci
npm run dev          # http://localhost:5173, proxies /api → localhost:8091
npm run typecheck    # tsc — run it before pushing; CI is not a typing service
```

The dev server proxies to a running stack, so start `./vms up -d` first. If
your API is on a non-default port: `API_PORT=8097 npm run dev`.

### Backend loop

The API rebuilds in seconds: `./vms up -d --build api`. Tests live next to the
service they cover and run with plain pytest:

```bash
python -m pytest services/camera-mgmt/tests -q
python -m pytest services/nvr/tests -q
```

If your machine lacks the Python deps, run them inside the api image — the
containers have everything the tests need.

## What a good change looks like here

**Read the surrounding code first.** This repository has an unusually strong
comment culture: comments record *why* — the incident, the constraint, the
alternative that was rejected — not what the next line does. A change that
deletes that context, or adds code whose reasoning lives only in the PR
description, will be asked to carry its reasoning in the file. Six months from
now the PR is forgotten; the comment is not.

Beyond that:

- **One concern per PR.** A fix and a refactor are two PRs. Drive-by
  reformatting of untouched lines makes review slower and history noisier —
  don't.
- **Open an issue before building a feature.** Not for bug fixes or small
  improvements — for anything with a design decision in it. It may already be
  planned, out of scope, or shaped differently than you'd guess.
- **Match the failure-mode discipline.** Background loops must survive
  anything; best-effort calls log and let a reconcile loop heal; user-facing
  errors are sentences, not stack traces. When your change can fail, the
  interesting review question is what happens when it does.
- **Tests where the area has them.** Recording, retention and the
  extension seam carry tests; extend them when you touch those paths. Areas
  without tests aren't a licence to skip them for new logic.
- **Honest UI.** This product never fabricates a reading: an unknown state
  renders as unknown, never as a plausible value. Keep it that way.

## Three hard rules

These are enforced by scripts, not reviewers, so you'll hit them as failures:

1. **No new binary files.** Images, PDFs, archives, fonts — the packaging
   check the maintainers run fails on any binary not on its allowlist.
   If you believe one belongs, say so in the PR and it can be allowlisted
   deliberately.
2. **No secrets, no footage.** No RTSP URLs with real credentials (including
   in docstrings and examples — this has happened), no camera exports, no
   audit logs. Scrub logs before attaching them anywhere.
3. **Vendored files are declared.** Anything under a `vendor/` directory must
   appear in `vendored-licenses.tsv` with its licence, or `gen-notice.sh`
   fails. Prefer npm/pip dependencies over vendoring.

## Pull requests

Fill in the template; it is short. Expect review to engage with substance —
"what happens when the camera is offline / the disk is full / two workers race
here" is the house style of question. CI must be green, `npm run typecheck`
clean for frontend changes, and the CLA signed before merge.

## Code of conduct

`CODE_OF_CONDUCT.md` (Contributor Covenant 2.1) applies everywhere the project
talks: issues, PRs, discussions, review comments.
