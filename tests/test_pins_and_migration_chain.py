"""Two merge accidents that each broke every VMS install on 2026-10-15 — caught here instead of on a site.

1. A package pinned twice in one requirements file (`Pillow==11.0.0` and, 25 lines later, `Pillow==12.3.0`): pip
   refuses the whole file, so the image never builds.
2. Two migrations with the same revision id, or two heads: branches number their migrations from the tip they forked
   from, so a merge can land a second "055" (or a parallel 053/054) and `alembic upgrade` fails at API start.
   Renumber the incoming one after the current head and add a fingerprint to backend/migrate.py for databases that
   already ran the old number.
"""
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[=<>!~]=|[<>])")


def test_no_package_pinned_twice_in_a_requirements_file():
    dupes = []
    for f in sorted(ROOT.glob("services/*/requirements*.txt")) + sorted(ROOT.glob("tests/requirements*.txt")):
        names = [m.group(1).lower().replace("_", "-") for line in f.read_text().splitlines()
                 if (m := _REQ.match(line.split("#", 1)[0]))]
        dupes += [f"{f.relative_to(ROOT)}: {n}" for n, c in Counter(names).items() if c > 1]
    assert not dupes, "pinned more than once (pip will refuse the file):\n" + "\n".join(dupes)


def test_every_migration_chain_is_one_line():
    rev_re = re.compile(r'^revision(?::[^=]*)?=\s*["\']([^"\']+)["\']', re.M)
    down_re = re.compile(r'^down_revision(?::[^=]*)?=\s*["\']?([^"\'\n]*)["\']?', re.M)
    problems = []
    for versions in sorted(ROOT.glob("services/**/migrations/versions")):
        revs, downs = [], []
        for f in versions.glob("*.py"):
            text = f.read_text()
            r = rev_re.search(text)
            if r:
                revs.append(r.group(1))
                d = down_re.search(text)
                downs.append((d.group(1).strip() if d else "") or None)
        dup = [r for r, c in Counter(revs).items() if c > 1]
        heads = set(revs) - {d for d in downs if d and d != "None"}
        if dup:
            problems.append(f"{versions.relative_to(ROOT)}: duplicate revision id(s) {sorted(dup)}")
        if len(heads) > 1:
            problems.append(f"{versions.relative_to(ROOT)}: {len(heads)} heads {sorted(heads)}")
    assert not problems, "\n".join(problems)
