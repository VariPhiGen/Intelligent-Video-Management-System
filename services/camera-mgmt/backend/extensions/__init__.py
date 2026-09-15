"""extensions — optional feature packages, discovered by presence.

WHY THIS EXISTS
---------------
Some of what this codebase can do is not part of the open core: it belongs to a
commercially licensed tier and ships from a different repository. The core must
therefore run correctly when that code is simply **not in the tree** — not
disabled by a flag, not stubbed out, absent.

The rule that makes this work, and the one that is easy to break later:

    NOTHING IN backend/ MAY NAME AN EXTENSION.

No import of a package under here, no string literal naming one, no entry in a
list. If core code names an extension, deleting that extension's directory
breaks the core's import — and the whole arrangement is decorative. This module
discovers whatever directories exist beside it and asks each to register itself.
That is the only reference, and it names nothing.

WHAT AN EXTENSION IS
--------------------
A directory beside this file, containing a package that exposes::

    def register(app) -> None

It is called once during startup, before the app serves traffic, and may mount
routers, declare capabilities, and register anything else the core exposes a
registration point for. If it raises, startup fails loudly — a half-registered
extension is worse than an absent one, because the UI would offer routes the
backend does not serve.

THIS IS A TRANSITIONAL SHAPE
----------------------------
In-process registration is stage one. The end-state is a separate service over
HTTP, and every registration point here
is deliberately shaped so a network client could satisfy it as easily as an
in-process import — no passing of ORM sessions, no reaching into core internals,
nothing that would have to be redesigned rather than moved.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


def discover() -> list[str]:
    """Names of the extension packages present in this directory."""
    here = Path(__file__).resolve().parent
    return sorted(
        m.name
        for m in pkgutil.iter_modules([str(here)])
        if m.ispkg and not m.name.startswith("_")
    )


def load_models() -> list[str]:
    """Import every present extension's ``models`` module, if it has one.

    Alembic builds ``target_metadata`` from whatever is registered on the
    declarative Base at the time env.py runs. Extension tables are declared in
    their own modules, so without this they are simply absent from the metadata
    — and ``alembic revision --autogenerate`` would emit a DROP for every one of
    them. That is not hypothetical: it was true for the ~4 hours between the seam
    landing and this function existing, and it is the kind of defect that fires
    later, in someone else's hands.

    Called by migrations/env.py. Nothing else should need it: the running app
    imports these models through the routers that use them.
    """
    seen: list[str] = []
    for name in discover():
        try:
            importlib.import_module(f"{__name__}.{name}.models")
        except ModuleNotFoundError:
            continue          # an extension need not own tables
        seen.append(name)
    return seen


def owned_tables() -> set[str]:
    """Every database table declared as owned by a present extension.

    The core's alembic environment excludes these from autogenerate. Without it
    the core chain would happily propose changes to tables it does not own —
    into the wrong chain — and, worse, propose dropping an extension's version
    table, which would destroy that extension's migration state. Both were
    observed on 2026-08-28 by running an autogenerate probe rather than assuming.
    """
    owned: set[str] = set()
    for name in discover():
        try:
            mod = importlib.import_module(f"{__name__}.{name}.models")
        except ModuleNotFoundError:
            continue
        owned |= set(getattr(mod, "OWNED_TABLES", ()) or ())
    return owned


def migrations():
    """Yield ``(name, migrate_callable)`` for every extension that has a chain.

    An extension owning tables owns their migrations too, in its own version
    table, so the two chains never constrain each other. Extensions without one
    are skipped silently — owning tables is optional.
    """
    for name in discover():
        module = importlib.import_module(f"{__name__}.{name}")
        fn = getattr(module, "migrate", None)
        if callable(fn):
            yield name, fn


def load(app) -> list[str]:
    """Import every extension present and let it register itself.

    Returns the names loaded, for /health and for the startup log — an operator
    should be able to see which tier is running without reading the image.
    """
    loaded: list[str] = []
    for name in discover():
        module = importlib.import_module(f"{__name__}.{name}")
        register = getattr(module, "register", None)
        if register is None:
            # Present but not an extension. Loud, because the likely cause is a
            # half-finished package that silently serves nothing.
            log.warning("extensions.no_register", extension=name)
            continue
        register(app)
        loaded.append(name)
        log.info("extensions.registered", extension=name)
    if not loaded:
        log.info("extensions.none", hint="open-core build")
    return loaded
