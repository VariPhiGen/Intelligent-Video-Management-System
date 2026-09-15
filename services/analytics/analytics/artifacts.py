"""artifacts.py — build the model files a candidate needs, once, and find them again.

EXPORTS ARE DERIVED, NOT SHIPPED. The packaging harness refuses binaries it
cannot account for, and a `.pt` or an OpenVINO IR is exactly that, so nothing
here is committed. Artifacts are produced at runtime from the weights the
deployment already has and cached under the models volume beside them — the
same posture `yolov8n.pt` already has, which downloads on first use.

CACHED ON THE SOURCE'S CONTENT, NOT ITS NAME. The cache key is the SHA-256 of
the source weights plus the export format and the ultralytics version. An
operator who replaces `variphi_anpr.pt` in place, keeping the filename, gets a
rebuild rather than yesterday's model wearing today's name — and that is not
hypothetical, since ANPR weights are exactly the kind of file that gets swapped
during tuning.

EXPORTING IS THE EXPENSIVE, FAILURE-PRONE STEP, so it is isolated here. It
downloads nothing but writes hundreds of megabytes, takes seconds to minutes,
and can fail for reasons that say nothing about whether the candidate is any
good — a missing `onnxslim`, a disk with no room. A failure is recorded against
the candidate and the run continues; it never propagates as an ingest fault.

WHAT THIS DELIBERATELY DOES NOT DO is decide anything. It builds what it is
asked for and reports what happened. Whether an artifact is fast enough, and
whether it is ACCURATE enough, are separate questions asked later by the
benchmark and the accuracy gate.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import hardware

log = logging.getLogger("analytics.artifacts")

#: Bumped when the export procedure changes in a way that makes previously
#: cached artifacts untrustworthy. Cheaper than reasoning about which ones.
EXPORT_VERSION = 1

_MANIFEST = "manifest.json"


@dataclass
class Artifact:
    """A built model file, or the record of why it could not be built."""
    ok: bool
    component: str
    export_format: str
    source: str
    path: Optional[str] = None
    #: Wall clock for the export itself. NOT an inference metric and never
    #: folded into one — see index/benchmark.py, which keeps load time separate
    #: from latency for the same reason.
    export_seconds: Optional[float] = None
    size_bytes: Optional[int] = None
    cached: bool = False
    #: Class names read back from the built artifact. Empty when the format
    #: does not carry them, which for a plate localiser is disqualifying.
    names: Optional[dict] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _cache_key(source: str, export_format: str, ultralytics_version: str) -> str:
    digest = hardware.file_digest(source) or "nodigest"
    return f"{export_format}-{ultralytics_version}-v{EXPORT_VERSION}-{digest[:16]}"


def artifact_root(models_dir: str = "/models") -> str:
    return os.path.join(models_dir, "artifacts")


def _read_manifest(path: str) -> Optional[dict]:
    try:
        with open(os.path.join(path, _MANIFEST), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_manifest(path: str, payload: dict) -> None:
    try:
        with open(os.path.join(path, _MANIFEST), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except OSError as exc:
        log.warning("could not write artifact manifest at %s: %s", path, exc)


def export(component: str, source: str, export_format: str,
           models_dir: str = "/models", force: bool = False) -> Artifact:
    """Build (or reuse) one exported artifact for `source`.

    Returns an Artifact either way. Nothing here raises: an export failure
    removes a candidate from consideration, and removing a candidate must never
    be able to take the service down with it.
    """
    try:
        import ultralytics
        from ultralytics import YOLO
    except Exception as exc:                                       # noqa: BLE001
        return Artifact(False, component, export_format, source,
                        error=f"ultralytics unavailable: {exc}")

    # A bare catalogue name (`yolov8n.pt`) is fetched by ultralytics on demand
    # and need not exist yet; a path (`/models/variphi_anpr.pt`) is operator-
    # supplied and nothing will fetch it. See candidates.resolvable_weights.
    if os.path.dirname(source) and not os.path.exists(source):
        return Artifact(False, component, export_format, source,
                        error=f"source weights not found: {source}")

    # RESOLVE BEFORE HASHING. The cache key is the source's CONTENT, so a name
    # that has not been downloaded yet has nothing to hash — and hashing the
    # name instead would let two different models sharing a filename collide in
    # the cache, which is precisely the failure the content key exists to stop.
    resolved = source
    if not os.path.exists(source):
        try:
            resolved = str(getattr(YOLO(source), "ckpt_path", "") or source)
        except Exception as exc:                                   # noqa: BLE001
            return Artifact(False, component, export_format, source,
                            error=f"could not resolve weights {source}: "
                                  f"{type(exc).__name__}: {exc}")
        if not os.path.exists(resolved):
            return Artifact(False, component, export_format, source,
                            error=f"ultralytics resolved {source} to {resolved}, "
                                  f"which is absent")

    key = _cache_key(resolved, export_format, ultralytics.__version__)
    target_dir = os.path.join(artifact_root(models_dir), component, key)

    if not force:
        manifest = _read_manifest(target_dir)
        if manifest and os.path.exists(manifest.get("path", "")):
            log.debug("artifact cache hit %s", target_dir)
            return Artifact(
                True, component, export_format, source,
                path=manifest["path"], size_bytes=manifest.get("size_bytes"),
                names=manifest.get("names"), cached=True,
                export_seconds=manifest.get("export_seconds"),
            )

    # Export into a scratch directory and move the result into place, so an
    # interrupted or failed export cannot leave a half-written artifact that
    # the next run treats as a cache hit.
    scratch = tempfile.mkdtemp(prefix="analytics-export-")
    started = time.monotonic()
    try:
        staged = os.path.join(scratch, os.path.basename(resolved))
        shutil.copy2(resolved, staged)
        produced = str(YOLO(staged).export(format=export_format, verbose=False))
        elapsed = round(time.monotonic() - started, 2)

        if not os.path.exists(produced):
            return Artifact(False, component, export_format, source,
                            error=f"export reported {produced}, which is absent")

        # Read the names back off the BUILT artifact rather than the source.
        # The question is not what the source knew, it is what survived — and
        # for a plate localiser a lost class map turns the plate filter
        # class-agnostic, sending whole vehicles to the recogniser.
        names: Optional[dict] = None
        try:
            names = {str(k): str(v)
                     for k, v in (getattr(YOLO(produced), "names", {}) or {}).items()}
        except Exception as exc:                                   # noqa: BLE001
            log.warning("built %s but could not read its class names: %s",
                        produced, exc)

        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
        os.makedirs(target_dir, exist_ok=True)
        # rstrip THE SEPARATOR FIRST. Ultralytics returns an OpenVINO export as
        # a DIRECTORY path with a trailing separator, and os.path.basename of
        # ".../yolov8n_openvino_model/" is the empty string — so `final` became
        # the cache directory itself. shutil.move then did the right thing
        # (the files landed in a proper subdirectory) while the RECORDED path
        # pointed one level too high, at a directory ultralytics cannot load.
        name = os.path.basename(produced.rstrip("/\\")) or "artifact"
        final = os.path.join(target_dir, name)
        shutil.move(produced, final)

        size = _tree_size(final)
        _write_manifest(target_dir, {
            "component": component, "export_format": export_format,
            "source": source, "resolved_source": resolved,
            "source_digest": hardware.file_digest(resolved),
            "ultralytics": ultralytics.__version__,
            "export_version": EXPORT_VERSION, "path": final,
            "size_bytes": size, "names": names, "export_seconds": elapsed,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        log.info("exported %s -> %s (%s, %.1f MB, %.1fs)",
                 component, export_format, final, size / 1e6, elapsed)
        return Artifact(True, component, export_format, source, path=final,
                        export_seconds=elapsed, size_bytes=size, names=names)
    except Exception as exc:                                       # noqa: BLE001
        return Artifact(False, component, export_format, source,
                        export_seconds=round(time.monotonic() - started, 2),
                        error=f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _tree_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def names_preserved(built: Optional[dict], source_names: dict) -> bool:
    """Did the export keep the class map the localiser filter depends on?

    Compared by VALUE, not by key: an exported artifact may key its classes as
    strings where torch keys them as ints, and treating that as a loss would
    reject a perfectly good candidate.
    """
    if not built:
        return False
    return (sorted(str(v) for v in built.values())
            == sorted(str(v) for v in source_names.values()))


def prune(models_dir: str = "/models", keep_keys: Optional[set[str]] = None) -> int:
    """Delete cached artifacts nothing references any more. Returns the count.

    Deliberately opt-in and never automatic. An artifact costs disk; deleting
    one that a future recalibration would have reused costs an export. Keeping
    them is the cheaper mistake, so this only runs when asked.
    """
    root = artifact_root(models_dir)
    if not os.path.isdir(root):
        return 0
    removed = 0
    for component in os.listdir(root):
        comp_dir = os.path.join(root, component)
        if not os.path.isdir(comp_dir):
            continue
        for key in os.listdir(comp_dir):
            if keep_keys is not None and key in keep_keys:
                continue
            shutil.rmtree(os.path.join(comp_dir, key), ignore_errors=True)
            removed += 1
    return removed
