"""Unit tests for the open-core / extension seam (backend/extensions).

The seam's contract is that deleting an extension's directory yields a core that
still imports, migrates, builds and boots. That property was verified by hand on
2026-08-28 and by the maintainers' packaging harness; these tests cover the
invariants underneath it, so a regression fails in CI rather than at release time.

Two of them are regression tests for bugs that ONLY appeared when the stack was
restarted and the UI loaded — see test_no_nav_item_uses_the_section_key and
test_posture_reads_core_settings_from_the_core. Both compiled, passed every
import check, and shipped a broken page.

These tests live in the CORE test suite deliberately: they must keep running in
the open-core build, where the compliance extension is absent. Every one of them
therefore has to pass BOTH with and without it.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import extensions  # noqa: E402

BACKEND = ROOT / "backend"
EXT_DIR = BACKEND / "extensions"
SPA = ROOT.parents[1] / "frontend-react" / "src"


# ── The loader itself ─────────────────────────────────────────────────────────

def test_discover_returns_only_packages():
    """Non-package entries and private dirs are never treated as extensions."""
    for name in extensions.discover():
        assert (EXT_DIR / name / "__init__.py").is_file()
        assert not name.startswith("_")


def test_every_discovered_extension_registers():
    """A directory that cannot register is a half-finished package, not a
    feature. load() warns rather than raising, but every extension we actually
    ship must expose register()."""
    import importlib
    for name in extensions.discover():
        mod = importlib.import_module(f"backend.extensions.{name}")
        assert callable(getattr(mod, "register", None)), f"{name} has no register()"


def test_load_models_is_idempotent_and_safe_without_extensions():
    """Called by migrations/env.py. Must not raise when nothing is installed,
    and must not blow up on a second call (SQLAlchemy would raise on a
    re-declared table if the module were re-executed)."""
    first = extensions.load_models()
    second = extensions.load_models()
    assert first == second


def test_owned_tables_are_registered_on_the_metadata():
    """The autogenerate hazard: a table in the database but not in the metadata
    makes `alembic revision --autogenerate` emit a DROP for it. Anything an
    extension claims to own must actually be on the Base by the time env.py has
    called load_models()."""
    from backend.db import Base
    extensions.load_models()
    for table in extensions.owned_tables():
        assert table in Base.metadata.tables, f"{table} is owned but not on the metadata"


def test_migrations_yields_callables():
    for name, fn in extensions.migrations():
        assert callable(fn), f"{name}.migrate is not callable"


# ── The invariant: core must never name an extension ──────────────────────────

def _core_python_files():
    for p in BACKEND.rglob("*.py"):
        if EXT_DIR in p.parents or p == EXT_DIR / "__init__.py":
            continue
        if "__pycache__" in p.parts:
            continue
        yield p


def test_no_core_module_imports_an_extension():
    """The whole seam rests on this. If core code imports a name from an
    extension, deleting that extension breaks the core's import and the
    arrangement is decorative."""
    offenders = []
    for path in _core_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = ",".join(a.name for a in node.names)
            if mod and "extensions." in mod:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {mod}")
    assert not offenders, "core modules importing an extension:\n" + "\n".join(offenders)


def test_extensions_package_itself_names_nothing():
    """extensions/__init__.py is the single reference point, and it discovers
    rather than naming. A literal extension name here would defeat the loader."""
    src = (EXT_DIR / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    for name in extensions.discover():
        assert name not in literals, f"the loader hard-codes the extension name {name!r}"


# ── Capability registration ───────────────────────────────────────────────────

def test_register_capability_adds_catalogue_and_defaults():
    from backend.services import policy
    key = "__test_cap__"
    policy.CAPABILITIES.pop(key, None)
    for role in policy.EDITABLE_ROLES:
        policy.DEFAULT_POLICY[role].pop(key, None)
    try:
        policy.register_capability(
            key, label="Test", hint="h", ui_only=False, defaults={"dpo": True},
        )
        assert policy.CAPABILITIES[key]["label"] == "Test"
        # Every editable role gets an explicit value; unlisted roles default to
        # False, which is the safe direction for a capability that grants access.
        for role in policy.EDITABLE_ROLES:
            assert key in policy.DEFAULT_POLICY[role]
        assert policy.DEFAULT_POLICY["dpo"][key] is True
        assert policy.DEFAULT_POLICY["viewer"][key] is False
    finally:
        policy.CAPABILITIES.pop(key, None)
        for role in policy.EDITABLE_ROLES:
            policy.DEFAULT_POLICY[role].pop(key, None)


def test_register_capability_is_idempotent():
    """Registration runs once per worker process; uvicorn starts four. A second
    call must not overwrite or duplicate."""
    from backend.services import policy
    key = "__test_cap_twice__"
    policy.CAPABILITIES.pop(key, None)
    try:
        policy.register_capability(key, label="First", hint="h")
        policy.register_capability(key, label="Second", hint="h")
        assert policy.CAPABILITIES[key]["label"] == "First"
    finally:
        policy.CAPABILITIES.pop(key, None)
        for role in policy.EDITABLE_ROLES:
            policy.DEFAULT_POLICY[role].pop(key, None)


def test_core_policy_names_no_extension_capability():
    """policy.py listed dsr_manage and evidence_export in the catalogue and the
    role matrix until 2026-08-28 — open-core code naming commercial features,
    and matrix toggles for routes that would not exist."""
    from backend.services import policy
    src = (BACKEND / "services" / "policy.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    literal_caps = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    literal_caps.add(k.value)
    for name in extensions.discover():
        import importlib
        mod = importlib.import_module(f"backend.extensions.{name}")
        registered = {
            n.args[0].value
            for n in ast.walk(ast.parse(Path(mod.__file__).read_text(encoding="utf-8")))
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", "") == "register_capability"
            and n.args and isinstance(n.args[0], ast.Constant)
        }
        leaked = registered & literal_caps
        assert not leaked, f"policy.py hard-codes extension capabilities: {leaked}"


# ── Migration chains ──────────────────────────────────────────────────────────

def _chain(versions_dir: Path):
    revs = {}
    for f in versions_dir.glob("*.py"):
        src = f.read_text(encoding="utf-8")
        r = re.search(r'^revision: str = "([^"]+)"', src, re.M)
        d = re.search(r'^down_revision: Union\[str, None\] = (None|"([^"]+)")', src, re.M)
        if r and d:
            revs[r.group(1)] = d.group(2)
    return revs


def _assert_linear(revs, label):
    assert revs, f"{label}: no revisions found"
    children = {}
    for rev, parent in revs.items():
        children.setdefault(parent, []).append(rev)
    bases = [r for r, p in revs.items() if p is None]
    heads = [r for r in revs if r not in children]
    dangling = [p for p in revs.values() if p is not None and p not in revs]
    forks = {p: c for p, c in children.items() if len(c) > 1}
    assert len(bases) == 1, f"{label}: expected one base, got {bases}"
    assert len(heads) == 1, f"{label}: expected one head, got {heads}"
    assert not dangling, f"{label}: dangling parents {dangling}"
    assert not forks, f"{label}: forked at {forks}"
    # every revision reachable from the base
    seen, cur = [], bases[0]
    while cur is not None:
        seen.append(cur)
        nxt = children.get(cur, [])
        cur = nxt[0] if nxt else None
    assert len(seen) == len(revs), f"{label}: {len(revs) - len(seen)} unreachable revision(s)"


def test_core_migration_chain_is_linear():
    """024 and 027 moved to the compliance extension; 025 was reparented onto
    023 and 028 onto 026 to close the gaps. A dangling parent here is
    `alembic upgrade head` dying with "Can't locate revision" on every start."""
    _assert_linear(_chain(ROOT / "migrations" / "versions"), "core")


def test_core_chain_does_not_contain_extension_revisions():
    core = _chain(ROOT / "migrations" / "versions")
    for name in extensions.discover():
        ext_versions = EXT_DIR / name / "migrations" / "versions"
        if not ext_versions.is_dir():
            continue
        overlap = set(core) & set(_chain(ext_versions))
        assert not overlap, f"{name}: revisions in both chains: {overlap}"


@pytest.mark.parametrize("name", extensions.discover())
def test_extension_migration_chain_is_linear(name):
    versions = EXT_DIR / name / "migrations" / "versions"
    if not versions.is_dir():
        pytest.skip(f"{name} owns no migrations")
    _assert_linear(_chain(versions), name)


@pytest.mark.parametrize("name", extensions.discover())
def test_extension_chain_uses_its_own_version_table(name):
    """Sharing alembic_version with the core would make each chain believe the
    other's head was its own."""
    mig = EXT_DIR / name / "migrations"
    if not mig.is_dir():
        pytest.skip(f"{name} owns no migrations")
    table = getattr(__import__(
        f"backend.extensions.{name}.migrations", fromlist=["VERSION_TABLE"]
    ), "VERSION_TABLE", None)
    assert table and table != "alembic_version", f"{name}: bad version table {table!r}"


def test_core_env_excludes_extension_tables_from_autogenerate():
    """Two hazards found by running an autogenerate probe on 2026-08-28: the
    core proposed DROPPING alembic_version_compliance (which would erase the
    extension's migration history), and proposed changes to extension-owned
    tables into the wrong chain."""
    src = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "include_object" in src, "core env.py has no include_object filter"
    assert "alembic_version" in src, "core env.py does not exclude version tables"
    assert "owned_tables" in src, "core env.py does not exclude extension tables"


# ── SPA seam ──────────────────────────────────────────────────────────────────

SPA_EXT = SPA / "extensions"


@pytest.mark.skipif(not SPA_EXT.is_dir(), reason="SPA sources not present")
def test_no_core_spa_module_imports_an_extension():
    """The frontend mirror of the backend invariant. A static import makes
    deleting the directory a build failure rather than a smaller build."""
    offenders = []
    for p in SPA.rglob("*.ts*"):
        if SPA_EXT in p.parents or "node_modules" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""from\s+['"](@/extensions/\w|\./extensions/\w|.*/extensions/\w)""", line):
                offenders.append(f"{p.relative_to(SPA)}:{i}: {line.strip()}")
    assert not offenders, "core SPA modules importing an extension:\n" + "\n".join(offenders)


@pytest.mark.skipif(not SPA_EXT.is_dir(), reason="SPA sources not present")
def test_no_nav_item_uses_the_section_key():
    """REGRESSION (2026-08-28). ExtensionNavItem originally placed itself with
    `section: 'Govern'`. Shell.tsx distinguishes rail headers from links with
    `'section' in item`, so the entry was rendered as a HEADER and then dropped
    by the empty-section filter — it vanished with no error, no warning, and
    nothing in any log. It compiled, and the bundle contained the right strings.

    The placement key must stay `underSection`, and no manifest may reintroduce
    a bare `section` on a nav entry."""
    iface = (SPA_EXT / "index.tsx").read_text(encoding="utf-8")
    assert "underSection" in iface, "ExtensionNavItem lost its underSection key"
    nav_block = re.search(r"interface ExtensionNavItem\s*\{(.*?)\n\}", iface, re.S)
    assert nav_block, "ExtensionNavItem interface not found"
    assert not re.search(r"^\s*section\??:", nav_block.group(1), re.M), \
        "ExtensionNavItem declares `section` — collides with Shell.tsx's header discriminator"

    for manifest in SPA_EXT.glob("*/index.tsx"):
        src = manifest.read_text(encoding="utf-8")
        nav = re.search(r"nav:\s*\[(.*?)\]", src, re.S)
        if nav:
            assert not re.search(r"\bsection\s*:", nav.group(1)), \
                f"{manifest.name}: nav entry uses `section` instead of `underSection`"


# ── Settings split ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", extensions.discover())
def test_extension_settings_do_not_shadow_core_fields(name):
    """REGRESSION (2026-08-28). Moving four tier-3 settings into the extension
    and repointing `settings` at the new object silently took the CORE fields
    away from posture.py, which reads from both tiers — a live HTTP 500:
    'ComplianceSettings' object has no attribute 'nvr_default_retention_days'.

    Every `settings.<field>` in an extension must resolve on whichever object
    that module actually imported as `settings`."""
    import importlib
    from backend.config import settings as core_settings

    pkg = EXT_DIR / name
    try:
        ext_cfg = importlib.import_module(f"backend.extensions.{name}.config")
    except ModuleNotFoundError:
        pytest.skip(f"{name} has no settings of its own")
    ext_fields = set(type(ext_cfg.settings).model_fields)
    core_fields = set(type(core_settings).model_fields)

    overlap = ext_fields & core_fields
    assert not overlap, f"{name}: settings shadow core fields {overlap}"

    missing = []
    for path in pkg.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        uses_ext = re.search(r"^from \.config import settings", src, re.M)
        for field in re.findall(r"\bsettings\.([a-z_][a-z0-9_]*)", src):
            available = ext_fields if uses_ext else core_fields
            if field not in available:
                missing.append(f"{path.relative_to(pkg)}: settings.{field}")
    assert not missing, (
        f"{name}: settings attribute(s) not on the imported settings object:\n"
        + "\n".join(missing)
    )
