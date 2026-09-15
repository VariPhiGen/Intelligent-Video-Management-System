"""migrate — bring the database up to date, core chain first, then extensions.

Run as ``python -m backend.migrate`` before the app starts. This replaced a bare
``alembic upgrade head`` in the compose command on 2026-08-28, when the tier-3
migrations moved into their own chain.

The alternative was to add a second ``alembic -c ...`` invocation to
docker-compose.yml. That would have put the name of a commercially-licensed
extension into the open core's deployment configuration — the same class of leak
as an ``if license.has_feature(...)`` branch, just in YAML. Here the core runs
its own chain and then asks whatever extensions exist to run theirs; it names
none of them.

Exit codes: 0 on success, non-zero if any chain fails. A failed migration must
stop the container rather than let the app start against a schema it does not
match — which is why this is `&&`-chained with uvicorn rather than backgrounded.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config

from backend import extensions

log = structlog.get_logger(__name__)

# alembic.ini sits at the service root, one level above the backend package.
_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def _core() -> None:
    cfg = Config(str(_INI))
    url = os.environ.get("DATABASE_URL")
    if url:
        cfg.set_main_option("sqlalchemy.url", url)
    # script_location in alembic.ini is relative to the service root.
    cfg.set_main_option("script_location", str(_INI.parent / "migrations"))
    command.upgrade(cfg, "head")
    log.info("migrate.core.ok")


def main() -> int:
    try:
        _core()
    except Exception:
        log.exception("migrate.core.failed")
        return 1

    failed = False
    for name, migrate in extensions.migrations():
        try:
            migrate()
            log.info("migrate.extension.ok", extension=name)
        except Exception:
            log.exception("migrate.extension.failed", extension=name)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
