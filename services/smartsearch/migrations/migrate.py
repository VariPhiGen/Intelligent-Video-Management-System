#!/usr/bin/env python3
"""migrate — bring the Smart Search index database up to date.

Plain ordered SQL, not alembic. The camera registry uses alembic because its
schema has 29 revisions and a tier-3 extension chain hanging off it; this
database has two tables and no branch. A migration runner you can read in one
screen is the right weight for that, and it keeps the search service from
depending on the registry's toolchain.

Each file in migrations/ named NNN_*.sql runs once, in order, inside a
transaction, and is recorded in schema_migrations. Re-running is a no-op.

    python3 migrations/migrate.py            # uses SEARCHDB_URL
    python3 migrations/migrate.py --status   # what is applied, what is pending
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent
DSN = os.environ.get(
    "SEARCHDB_URL",
    "postgresql://search:search_secret@127.0.0.1:5434/smartsearch",
)

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text        PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


def files() -> list[Path]:
    return sorted(p for p in HERE.glob("[0-9][0-9][0-9]_*.sql"))


def applied(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP)
        cur.execute("SELECT version FROM schema_migrations")
        return {r[0] for r in cur.fetchall()}


def main(argv: list[str]) -> int:
    pending_only = "--status" in argv
    try:
        conn = psycopg.connect(DSN, autocommit=False)
    except psycopg.Error as exc:
        # The DSN carries a password; psycopg does not echo it, but say plainly
        # which variable to look at rather than printing a raw libpq error.
        print(f"migrate: cannot connect (check SEARCHDB_URL): {exc}", file=sys.stderr)
        return 2

    with conn:
        done = applied(conn)
        conn.commit()
        todo = [p for p in files() if p.name not in done]

        if pending_only:
            for p in files():
                print(f"  {'applied' if p.name in done else 'PENDING':>7}  {p.name}")
            return 0

        if not todo:
            print(f"migrate.ok  already at head ({len(done)} applied)")
            return 0

        for p in todo:
            sql = p.read_text(encoding="utf-8")
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version) VALUES (%s)", (p.name,)
                    )
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                # Stop at the first failure. A half-applied chain that keeps
                # going is how you get a schema nobody can reason about.
                print(f"migrate.failed  {p.name}: {exc}", file=sys.stderr)
                return 1
            print(f"migrate.applied  {p.name}")

    print(f"migrate.ok  {len(todo)} applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
