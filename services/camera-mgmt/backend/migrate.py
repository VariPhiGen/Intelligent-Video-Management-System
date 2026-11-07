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


# ---- One-time repair: appliances migrated on the OLD `field` numbering ---------------------------------
# `field` (deployed by a cloud fleet console) and the ai line both created migrations 033/035/036/050/051/
# 052 with DIFFERENT contents. On the merged chain those ids mean the ai migrations, and field's own
# moved: datasets/PTZ -> 045/046 (identical DDL), ONVIF-HTTPS/registration/branding -> 053-056. An appliance
# whose alembic_version still holds an OLD field id would otherwise be treated as already past ai's 033-052
# and skip them all (missing rules / bridge / heatmap / retention tables -> 500s).
#
# Fingerprint, not the id alone (the id is ambiguous by construction): the old field chain created
# `datasets` from its 033 on, and never `alert_rules` (ai 039). The ai chain has no `datasets` before 045 and
# always has `alert_rules` from 039. So: collision id + datasets + no alert_rules == old field lineage.
# Repair = full pg_dump (fail CLOSED: no backup, no repair, the API does not start), then stamp 032 — the last
# id both chains share — and let `upgrade head` replay forward; the re-run migrations are existence-guarded,
# so nothing that exists is recreated and no data is dropped. Restoring the dump undoes all of it.
_OLD_FIELD_IDS = {"033", "035", "036", "050", "051", "052"}


# ---- One-time repair: appliances that ran the `manual` branch's 053/054 (QA sink) -----------------------------
# `manual` numbered its QA-sink migrations 053/054 after 052, the same ids `field` already used for ONVIF-HTTPS
# and camera registration. On the merged chain they are 058/059 (2026-10-15). A `manual` database stamped 053/054
# would otherwise be read as past field's 053/054 and never get onvif_use_https / camera_registrations.
# Fingerprint: `qa_sink_config` exists while the stamp is 053 or 054 — on the merged chain that table first
# appears at 058, so the pair only occurs on the `manual` numbering. Same repair as above, to 052: backup first,
# then 053-059 replay, each existence-guarded.
_MANUAL_QA_IDS = {"053", "054", "055"}   # 055 = its labelled-image column (now 060)


def _libpq_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _lineage(url: str) -> dict:
    import asyncpg
    conn = await asyncpg.connect(_libpq_url(url))
    try:
        has_ver = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ver = await conn.fetchval("SELECT version_num FROM alembic_version LIMIT 1") if has_ver else None
        datasets = await conn.fetchval("SELECT to_regclass('public.datasets') IS NOT NULL")
        rules = await conn.fetchval("SELECT to_regclass('public.alert_rules') IS NOT NULL")
        qa = await conn.fetchval("SELECT to_regclass('public.qa_sink_config') IS NOT NULL")
        return {"version": ver, "datasets": bool(datasets), "alert_rules": bool(rules), "qa_sink": bool(qa)}
    finally:
        await conn.close()


def is_old_field_lineage(state: dict) -> bool:
    """Pure: does this database carry the pre-merge `field` migration numbering?"""
    return (str(state.get("version") or "") in _OLD_FIELD_IDS
            and bool(state.get("datasets")) and not state.get("alert_rules"))


def is_manual_qa_lineage(state: dict) -> bool:
    """Pure: does this database carry the pre-merge `manual` numbering (QA sink as 053/054)?"""
    return str(state.get("version") or "") in _MANUAL_QA_IDS and bool(state.get("qa_sink"))


def repair_target(state: dict) -> str | None:
    """Pure: the id to re-stamp to before `upgrade head`, or None when the chain is already right."""
    if is_old_field_lineage(state):
        return "032"
    if is_manual_qa_lineage(state):
        return "052"
    return None


def _backup(url: str) -> str:
    import shutil
    import subprocess
    import time
    if not shutil.which("pg_dump"):
        raise RuntimeError("pg_dump is not installed in this image — refusing to re-stamp without a backup")
    out_dir = Path(os.environ.get("VMS_DB_BACKUP_DIR", "/evidence-exports/db-backups"))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"premerge-{time.strftime('%Y%m%d-%H%M%S')}.dump"
    # The URL carries the password: pass it to pg_dump only, never to the log.
    res = subprocess.run(["pg_dump", "--format=custom", "--no-owner", f"--file={path}",
                          f"--dbname={_libpq_url(url)}"], capture_output=True, text=True, timeout=3600)
    if res.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f"pg_dump failed (exit {res.returncode}): {res.stderr.strip()[-300:]}")
    os.chmod(path, 0o600)
    return str(path)


async def _restamp(url: str, to: str = "032") -> None:
    import asyncpg
    conn = await asyncpg.connect(_libpq_url(url))
    try:
        await conn.execute("UPDATE alembic_version SET version_num = $1", to)
    finally:
        await conn.close()


def _repair_old_field_lineage() -> None:
    import asyncio
    url = os.environ.get("DATABASE_URL")
    if not url:
        return
    state = asyncio.run(_lineage(url))
    target = repair_target(state)
    if target is None:
        return
    lineage = "old_field_lineage" if target == "032" else "manual_qa_lineage"
    path = _backup(url)
    # Restore with THIS image's pg_restore (same version as the pg_dump that wrote it). Against the postgres:15
    # server it reports one ignorable error, `unrecognized configuration parameter "transaction_timeout"` (a
    # newer-client SET) — the restore itself completes; verified on 2026-10-07 (052 state, data intact).
    log.warning(f"migrate.repair.{lineage}", old_version=state["version"], backup=path,
                restore_hint=("pg_restore --clean --if-exists --no-owner -d <db> " + path
                              + "  (one 'transaction_timeout' error is expected and harmless)"))
    asyncio.run(_restamp(url, target))
    log.warning("migrate.repair.restamped", to=target)


def _core() -> None:
    _repair_old_field_lineage()
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
