#!/usr/bin/env python3
"""coverage_ledger.py — regenerate the numbers in TESTING.md.

The test programme's ledger used to be hand-derived: someone counted, wrote the
figures into a document, and the document began going stale the moment the next
test landed. It went stale by three E2E journeys inside a single afternoon.

So the document holds judgement — what to test, what to skip, why — and this
script holds the arithmetic. Run it and paste, or run it with --check in CI to
find out that the committed table no longer matches the tree.

    python scripts/coverage_ledger.py            # markdown table to stdout
    python scripts/coverage_ledger.py --json     # same data, machine-readable
    python scripts/coverage_ledger.py --check    # exit 1 if TESTING.md is stale

Counting rules, kept deliberately crude and therefore stable:

  source lines  every .py/.ts/.tsx under the service, MINUS tests, migrations,
                __pycache__, node_modules and generated clients. Blank lines and
                comment-only lines are excluded — a docstring-heavy file should
                not read as well-covered.
  test lines    the same measure over the tests directory only.
  tests         `def test_*` for pytest, `it(`/`test(` for vitest and Playwright.
                Parametrised cases count once, as written, because the number is
                a measure of intent rather than of executions.

The ratio is test lines per source line. It is a shape indicator, NOT a target:
see the standing decision in TESTING.md against a repo-wide
coverage gate. A service can move down this table by being written well.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "TESTING.md"

# Services in the order they appear in compose, not ranked — sorting is done at
# render time so the table can be read as a league without the source lying
# about priority.
SERVICES = ["analytics", "smartsearch", "frames", "camera-mgmt", "nvr", "motion"]

SRC_EXT = {".py", ".ts", ".tsx"}
SKIP_DIRS = {
    "tests", "test", "__pycache__", "node_modules", "migrations", "versions",
    "dist", "build", ".venv", "venv", "test-results", "playwright-report",
}

PY_TEST = re.compile(r"^\s*(?:async\s+)?def\s+test_")
JS_TEST = re.compile(r"^\s*(?:it|test)\s*\(")


def significant_lines(path: Path) -> int:
    """Lines that are neither blank nor a whole-line comment."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    n = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", "//", "/*", "*", "*/")):
            continue
        n += 1
    return n


def walk(base: Path, *, want_tests: bool) -> list[Path]:
    """Every source file under base. want_tests picks the tests tree instead."""
    if not base.is_dir():
        return []
    out = []
    for p in base.rglob("*"):
        if p.suffix not in SRC_EXT or not p.is_file():
            continue
        parts = set(p.parts)
        in_tests = bool(parts & {"tests", "test"}) or p.name.endswith(
            (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
        )
        if want_tests != in_tests:
            continue
        if parts & (SKIP_DIRS - {"tests", "test"} if want_tests else SKIP_DIRS):
            continue
        out.append(p)
    return out


def count_tests(files: list[Path]) -> int:
    n = 0
    for p in files:
        pattern = PY_TEST if p.suffix == ".py" else JS_TEST
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if pattern.match(line):
                    n += 1
        except OSError:
            continue
    return n


def measure(name: str, base: Path) -> dict:
    src_files = walk(base, want_tests=False)
    test_files = walk(base, want_tests=True)
    src = sum(significant_lines(p) for p in src_files)
    tst = sum(significant_lines(p) for p in test_files)
    return {
        "name": name,
        "src_lines": src,
        "test_lines": tst,
        "ratio": round(tst / src, 3) if src else 0.0,
        "tests": count_tests(test_files),
        "test_files": len(test_files),
    }


def collect() -> dict:
    rows = [measure(s, ROOT / "services" / s) for s in SERVICES]
    rows.append(measure("frontend-react", ROOT / "frontend-react" / "src"))
    rows.sort(key=lambda r: r["ratio"], reverse=True)

    # Repo-level and E2E are counted but kept OUT of the per-service table:
    # neither has a "source" denominator that means anything. tests/ asserts
    # structural invariants over compose files it does not own, and e2e/ drives
    # the whole stack from outside.
    repo = walk(ROOT / "tests", want_tests=True)
    e2e = walk(ROOT / "e2e" / "tests", want_tests=True)
    extras = {
        "repo_structural": count_tests(repo),
        "e2e_journeys": count_tests(e2e),
        "e2e_specs": sorted(p.name for p in e2e),
    }
    total_src = sum(r["src_lines"] for r in rows)
    total_tst = sum(r["test_lines"] for r in rows)
    return {
        "rows": rows,
        "extras": extras,
        "total": {
            "src_lines": total_src,
            "test_lines": total_tst,
            "ratio": round(total_tst / total_src, 3) if total_src else 0.0,
            "tests": sum(r["tests"] for r in rows),
            "grand_total": sum(r["tests"] for r in rows)
            + extras["repo_structural"]
            + extras["e2e_journeys"],
        },
    }


def render(data: dict) -> str:
    lines = [
        "| Service | Src lines | Test lines | Ratio | Tests |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in data["rows"]:
        lines.append(
            f"| {r['name']} | {r['src_lines']:,} | {r['test_lines']:,} "
            f"| {r['ratio']:.3f} | {r['tests']} |"
        )
    t = data["total"]
    lines.append(
        f"| **total** | **{t['src_lines']:,}** | **{t['test_lines']:,}** "
        f"| **{t['ratio']:.3f}** | **{t['tests']}** |"
    )
    x = data["extras"]
    lines.append("")
    lines.append(
        f"Plus {x['repo_structural']} repo-level structural tests and "
        f"{x['e2e_journeys']} E2E assertions across "
        f"{len(x['e2e_specs'])} journey specs "
        f"({', '.join(s.split('-')[0].upper() for s in x['e2e_specs']) or 'none'}). "
        f"**Grand total {t['grand_total']}.**"
    )
    return "\n".join(lines)


BEGIN = "<!-- LEDGER:BEGIN -->"
END = "<!-- LEDGER:END -->"


def splice(doc_text: str, table: str) -> str:
    """Replace whatever sits between the ledger markers."""
    if BEGIN not in doc_text or END not in doc_text:
        raise SystemExit(f"{DOC} is missing the {BEGIN} / {END} markers")
    head, rest = doc_text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{BEGIN}\n{table}\n{END}{tail}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="emit raw data")
    ap.add_argument("--write", action="store_true", help="splice into the doc")
    ap.add_argument("--check", action="store_true", help="exit 1 if doc is stale")
    args = ap.parse_args()

    data = collect()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    table = render(data)
    if args.write or args.check:
        current = DOC.read_text(encoding="utf-8")
        updated = splice(current, table)
        if args.check:
            if updated != current:
                print(
                    "TESTING.md ledger is out of date.\n"
                    "Run: python scripts/coverage_ledger.py --write",
                    file=sys.stderr,
                )
                return 1
            print("ledger up to date")
            return 0
        DOC.write_text(updated, encoding="utf-8")
        print(f"updated {DOC.relative_to(ROOT)}")
        return 0

    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
