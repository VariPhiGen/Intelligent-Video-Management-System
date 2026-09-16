# CLA smoke test — throwaway

This file exists only to give a pull request something to change, so that the
CLA assistant workflow can be proven to work end to end **before** a real
contribution depends on it.

What is being verified:

1. `.github/workflows/cla.yml` triggers on `pull_request_target`
2. `CLA_SIGNATURES_TOKEN` is present on THIS repository (secrets do not travel
   between repositories, and this workflow was written before this repository
   had any content)
3. That token can write to the `cla-signatures` branch — the default
   `GITHUB_TOKEN` cannot, which is the whole reason the secret exists
4. Signing appends to `signatures/v1/cla.json` on that branch

The pull request carrying this file is meant to be **closed, not merged**, and
this file deleted with it. If you are reading it on `main`, that did not happen.
