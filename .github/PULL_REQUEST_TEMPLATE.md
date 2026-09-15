<!-- One concern per PR. A fix and a refactor are two PRs. -->

## What this changes and why

<!-- The problem, then the change. If there's a design decision in here, say
     what you rejected and why — that reasoning should also live as a comment
     in the code, where the next reader will actually find it. -->

Fixes #

## How it was tested

<!-- Commands and results, not adjectives. e.g.:
     - python -m pytest services/nvr/tests -q  → 14 passed
     - npm run typecheck                       → clean
     - manual: added an H.265 camera via the wizard, verified playback -->

## Checklist

- [ ] Read the file(s) I changed, not just the lines — the change fits the
      surrounding reasoning and keeps its comments truthful
- [ ] Tests pass in the areas that have them; new logic that can fail has a
      test for its failure mode
- [ ] `npm run typecheck` is clean (frontend changes)
- [ ] No new binary files, no credentials or private addresses anywhere —
      including docstrings, examples, and attached logs
- [ ] Anything vendored is declared in `vendored-licenses.tsv`
- [ ] CLA signed (the bot will prompt on your first PR — see `CLA.md`)
