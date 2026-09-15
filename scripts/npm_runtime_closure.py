#!/usr/bin/env python3
"""The RUNTIME closure of an npm lockfile — what actually reaches an image.

WHY THIS EXISTS. The notices reminder fails a pull request that touches a
dependency-defining file without updating THIRD-PARTY-NOTICES.md. That is the
right default for `requirements.txt` and every Dockerfile, because those change
what a published image contains.

`frontend-react/package.json` is the one case where it is not. The SPA is built
in a throwaway stage:

    FROM node:22-slim AS ui-build
    RUN npm ci && npm run build
    ...
    FROM python:3.11-slim
    COPY --from=ui-build /ui/dist/ frontend_dist/

Only `dist/` crosses into the runtime image. Node never ships, and neither does
a single devDependency — vitest, jsdom, Testing Library, TypeScript. Adding a
test framework therefore cannot change what the notices describe, and failing a
PR for it is a check going red for a reason outside the author's change, which
is the thing this repository has already decided it will not tolerate (see the
header of notices.yml).

WHAT IT DOES NOT DO. It does not simply trust `devDependencies`. A lockfile can
change a TRANSITIVE runtime version with no edit to package.json at all — an
`npm update` or an audit fix — and that genuinely does alter the image. So this
reads the lock and prints the closure of everything NOT marked dev-only, plus
the root's declared runtime `dependencies`. Compare the output across two
revisions: identical means nothing that ships changed, and only then is the
reminder safe to skip.

Lockfile v3 (`npm` 7+) marks dev-only entries with `"dev": true`, and entries
reachable both ways with `"devOptional"`. Anything else is production.

Stdlib only, deliberately: this runs in a job that installs nothing, so it
cannot go red because a wheel moved.

Usage:
    python3 scripts/npm_runtime_closure.py < package-lock.json
    git show "$base:frontend-react/package-lock.json" | python3 scripts/npm_runtime_closure.py
"""
from __future__ import annotations

import json
import sys


def runtime_closure(lock: dict) -> list[str]:
    """Every package that can reach a runtime image, as sorted `name@version`.

    The root entry ("") carries the declared `dependencies` map. It is included
    verbatim so that adding a runtime dependency shows up even in the unlikely
    case that its resolved entry is unchanged.
    """
    packages = lock.get("packages")
    if packages is None:
        raise SystemExit(
            "not a lockfileVersion 2/3 package-lock.json (no `packages` key). "
            "This check reads the `dev` markers that only those versions carry."
        )

    root = packages.get("", {})
    lines = [
        "root:dependencies="
        + json.dumps(root.get("dependencies", {}), sort_keys=True)
    ]
    lines += sorted(
        f"{name}@{meta.get('version', '')}"
        for name, meta in packages.items()
        # `name` is "" for the root entry, handled above.
        if name and not meta.get("dev") and not meta.get("devOptional")
    )
    return lines


def main() -> None:
    try:
        lock = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"could not parse the lockfile: {exc}") from exc
    print("\n".join(runtime_closure(lock)))


if __name__ == "__main__":
    main()
