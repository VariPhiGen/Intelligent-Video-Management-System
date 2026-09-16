"""face_weights.py — fetch the OpenCV Zoo face models on first use.

WHY THIS EXISTS. Every other model on this appliance arrives on its own:
ultralytics fetches `yolov8n.pt`, and the plate localiser and reader are
fetched by their own libraries. The two face models were the exception — they
were pointed at by absolute path and documented as a manual `curl` plus
`docker cp` in services/smartsearch/README.md. Nothing in the product performed
that step, and nothing told an operator it was outstanding: on a clean install
face search simply reported `available: false` for ever while person and
vehicle search worked, which reads as a broken feature rather than an
unfinished install.

WHY NOT BAKE THEM INTO THE IMAGE. The open-core packaging harness refuses any
binary it cannot account for (scripts/as-if-open-core.sh, the binary
allowlist), so a committed `.onnx` cannot ship. Downloading on first use is the
same answer the detector and the plate models already give, and it keeps the
weights in the shared `search_models` volume where both services see them.

WHAT THIS GUARANTEES, and it is the whole reason it verifies rather than just
downloads: what lands in /models is the file whose digest is recorded here, or
nothing at all. A truncated download, a captive-portal login page, or an
upstream file that changed under us all produce a digest mismatch and are
discarded — never left on disk where the next start would load them as a model.

NEVER RAISES. A blocked egress leaves the files absent, which is precisely the
state every caller already handles (`build()` returns an inactive encoder
carrying the reason). Face search stays unavailable; nothing else is affected.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

#: Upstream, keyed by the FILENAME the config points at. Keying on the basename
#: rather than the full path is deliberate: an operator who repoints
#: SEARCH_FACE_WEIGHTS at their own file gets their file, untouched. We only
#: ever fetch the two names this product ships defaults for.
#:
#: The ref is `main`, matching the documented manual command so both paths
#: fetch identical bytes. The digest is what makes that safe — if upstream ever
#: republishes these files the fetch fails closed, with a message naming the
#: digest to update, rather than silently installing something else.
_BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
_MODELS: dict[str, tuple[str, str]] = {
    "face_detection_yunet_2023mar.onnx": (
        f"{_BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "face_recognition_sface_2021dec.onnx": (
        f"{_BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}

#: SFace is 37 MB, and this runs on an appliance LAN that may be slow rather
#: than blocked. Long enough not to give up on a working-but-slow link;
#: bounded, because the caller is a warm-up thread and not a request.
_TIMEOUT_SEC = 120

#: One attempt per file per process. The engines already have their own
#: "load failed, stop respawning" flags, but this service is not the only
#: caller: without it, a face query on an egress-blocked box would re-attempt a
#: 37 MB download on every search.
_attempted: set[str] = set()


def _env_off(*names: str) -> bool:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip().lower() in ("0", "false", "no", "off"):
            return True
    return False


def _download(url: str, digest: str, path: str) -> bool:
    """Fetch `url` to `path`, atomically, only if it hashes to `digest`."""
    target_dir = os.path.dirname(path) or "."
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        log.warning("face weights: cannot create %s: %s", target_dir, exc)
        return False

    sha = hashlib.sha256()
    tmp = None
    try:
        # Same directory as the target so the rename below is atomic — /models
        # is a volume and /tmp is not on it, and os.replace across filesystems
        # is not atomic (it is not even permitted on some).
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".face-weights-")
        with os.fdopen(fd, "wb") as out:
            with urllib.request.urlopen(url, timeout=_TIMEOUT_SEC) as resp:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    sha.update(chunk)
                    out.write(chunk)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("face weights: download failed for %s: %s — face search "
                    "stays unavailable until it succeeds", os.path.basename(path), exc)
        _unlink(tmp)
        return False

    got = sha.hexdigest()
    if got != digest:
        log.error("face weights: %s downloaded but hashes %s, expected %s. "
                  "Discarded — upstream may have republished the file; if so "
                  "the expected digest in face_weights.py needs updating.",
                  os.path.basename(path), got, digest)
        _unlink(tmp)
        return False

    try:
        # Both services mount the same volume and may fetch at once. Whoever
        # renames last wins, and both wrote identical verified bytes, so the
        # race has no bad outcome — unlike writing the target in place, where a
        # reader could load a half-written model.
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("face weights: could not install %s: %s", path, exc)
        _unlink(tmp)
        return False

    log.info("face weights: fetched %s (%d bytes, sha256 verified)",
             os.path.basename(path), os.path.getsize(path))
    return True


def _unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def ensure(*paths: str, enabled: bool = True) -> None:
    """Make sure each known model file exists, fetching it if it does not.

    Returns nothing and reports nothing: the caller's next step is the
    `os.path.isfile` check it already performs, which is the honest answer
    whether or not anything was downloaded here.
    """
    if not enabled or _env_off("SEARCH_FACE_AUTO_DOWNLOAD",
                               "ANALYTICS_FACE_AUTO_DOWNLOAD"):
        return
    for path in paths:
        if not path or os.path.isfile(path):
            continue
        spec = _MODELS.get(os.path.basename(path))
        if spec is None:
            # An operator-supplied path. Nothing upstream to fetch, and
            # guessing a URL from a filename is how you install the wrong model.
            continue
        if path in _attempted:
            continue
        _attempted.add(path)
        url, digest = spec
        log.info("face weights: %s is missing — fetching from %s",
                 os.path.basename(path), url)
        _download(url, digest, path)
