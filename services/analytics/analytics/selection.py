"""selection.py — which detector implementation to load, and what to do when it fails.

WHY THIS IS A FILE AND NOT AN `if` IN models.py. The rule has three parts that
each cost something to get wrong: a hardware-dependent preference, a bounded
retry, and a fallback that must never leave the service without a detector. Put
inline, the third one is the one that gets dropped.

THE PREFERENCE IS MEASURED, NOT ASSUMED. On the reference appliance, live
1920x1080 camera frames, 15 timed rounds after a discarded warm-up:

    torch      p50 40.8ms   p95 62.0ms    23.9 fps
    openvino   p50 17.6ms   p95 21.8ms    55.4 fps
    onnx       p50 89.4ms   p95 215.9ms   10.0 fps

So OpenVINO is preferred on CPU: 2.3x faster and, more usefully for a bounded
queue, its p95 is better than torch's p50. ONNX Runtime is not offered at all —
it matched torch on a developer laptop and was twice as slow in the container,
which is exactly why "ONNX is fast on CPU" is not a safe thing to assume.

KEEP THE PRIZE IN PROPORTION. Measured on the same appliance at 16 cameras, the
detector accounted for 0.20 of 4.70 cores — about 4% of the container's CPU,
the rest being RTSP decode and the motion gate. Moving to OpenVINO saves
roughly 2% of the total. It is worth taking because it is free once built, but
it is not the lever that decides how many cameras fit; see scripts/capacity.py.

FALLING BACK MUST NOT COST INGEST. A failed OpenVINO load drops straight
through to torch inside the SAME warm-up, so the pipeline comes up either way.
The alternative — failing the warm-up and letting the pool's 30 s backoff retry
it — would leave a site not indexing for a minute or more because of an
optimisation it never asked for. The attempt count only decides how many FUTURE
warm-ups still bother trying.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from . import artifacts
from .hardware import resolve_device, runtimes

log = logging.getLogger("analytics.selection")

TORCH = "torch"
OPENVINO = "openvino"
AUTO = "auto"

#: Backends a deployment may ask for by name. ONNX is deliberately absent —
#: see the module docstring; it is slower than the incumbent here, so offering
#: it would only let an operator choose worse.
CHOOSABLE = (AUTO, OPENVINO, TORCH)


@dataclass
class Plan:
    """One attempt: which backend, and the artifact it needs."""
    backend: str
    #: Weights for torch; the exported IR directory for OpenVINO.
    artifact: str
    #: Why this backend is being tried, carried into /health.
    reason: str
    #: True when this is not the operator's or the machine's first choice.
    is_fallback: bool = False
    #: True when an operator pinned this backend by name rather than the
    #: rule choosing it. Reported so /health can distinguish 'someone asked
    #: for this' from 'the rule picked it'.
    is_pinned: bool = False


@dataclass
class SelectorState:
    """What has been tried and what happened. Reported, not just logged."""
    preferred: str = AUTO
    resolved_preference: str = TORCH
    attempts: int = 0
    max_attempts: int = 3
    failures: list[str] = field(default_factory=list)
    exhausted: bool = False
    last_export_seconds: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "configured": self.preferred,
            "preferred": self.resolved_preference,
            "attempts_used": self.attempts,
            "max_attempts": self.max_attempts,
            "exhausted": self.exhausted,
        }
        if self.failures:
            # Kept in full rather than counted: "it failed 3 times" tells an
            # operator nothing they can act on, and the reasons are usually
            # different from each other.
            out["failures"] = list(self.failures)
        if self.last_export_seconds is not None:
            out["last_export_seconds"] = self.last_export_seconds
        return out


class DetectorSelector:
    """Decides the detector backend, remembers failures, and gives up gracefully.

    One per ModelPool. Thread-safe because a warm-up runs on its own thread
    while /health reads the state from another.
    """

    def __init__(self, models_cfg) -> None:
        self._cfg = models_cfg
        self._lock = threading.Lock()
        #: (backend, reason), memoised by _ensure_resolved.
        self._resolved: Optional[tuple[str, str]] = None
        configured = (models_cfg.detector_backend or AUTO).strip().lower()
        if configured not in CHOOSABLE:
            # A typo must not silently downgrade a site to the slow path, and
            # must not take the service down either. Say so, take the default.
            log.error("unknown models.detector_backend %r — expected one of %s; "
                      "falling back to %s", configured, ", ".join(CHOOSABLE), AUTO)
            configured = AUTO
        self._state = SelectorState(
            preferred=configured,
            max_attempts=max(0, int(getattr(models_cfg, "backend_attempts", 3))),
        )

    # ── the preference ───────────────────────────────────────────────────────
    def _ensure_resolved(self) -> tuple[str, str]:
        """Resolve the preference once, and memoise it.

        CALLED FROM snapshot() AS WELL AS plans(), because /health is answered
        long before the first camera arrives and the answer must not depend on
        whether a warm-up has happened yet. Without this, a CPU appliance that
        had not yet indexed anything reported `preferred: torch` — the default
        field value — on a machine that in fact prefers OpenVINO.
        """
        if self._resolved is None:
            self._resolved = self._resolve_preference()
            self._state.resolved_preference = self._resolved[0]
        return self._resolved

    def _resolve_preference(self) -> tuple[str, str]:
        """(backend, reason) for this machine, ignoring past failures."""
        configured = self._state.preferred
        if configured == TORCH:
            return TORCH, "pinned by configuration"
        if configured == OPENVINO:
            return OPENVINO, "pinned by configuration"

        device = resolve_device(self._cfg.device)
        if device != "cpu":
            # The OpenVINO build here reports a CPU device only, and the
            # torch/cuda path is already far past what this workload needs.
            return TORCH, f"device is {device}; OpenVINO here is CPU-only"
        rt = runtimes()
        if not rt.get(OPENVINO) or not rt[OPENVINO].available:
            return TORCH, "openvino is not installed in this image"
        return OPENVINO, "CPU device; OpenVINO measured 2.3x faster than torch"

    # ── the plans to try, in order ───────────────────────────────────────────
    def plans(self, weights: str) -> list[Plan]:
        """Every implementation worth attempting this warm-up, best first.

        ALWAYS ENDS WITH TORCH. It needs no export, no extra runtime and no
        artifact cache, so it is the one thing that can be relied on to load —
        and a detector that fails to load is a service that indexes nothing.
        """
        with self._lock:
            preference, reason = self._ensure_resolved()
            exhausted = self._state.exhausted
            attempts = self._state.attempts
            budget = self._state.max_attempts

        torch_plan = Plan(TORCH, weights, "the implementation that always loads",
                          is_fallback=preference != TORCH)
        pinned = self._state.preferred != AUTO
        if preference == TORCH:
            return [Plan(TORCH, weights, reason, is_pinned=pinned)]
        if exhausted or attempts >= budget:
            return [Plan(TORCH, weights,
                         f"{preference} failed {attempts}x; not retried")]

        artifact = self._artifact_for(preference, weights)
        if artifact is None:
            return [torch_plan]
        return [Plan(preference, artifact, reason, is_pinned=pinned),
                torch_plan]

    def _artifact_for(self, backend: str, weights: str) -> Optional[str]:
        """Build (or reuse) the exported model. None means it could not be had.

        An export failure is counted as an attempt: it is one of the ways the
        preferred backend does not work on this machine, and repeating a build
        that has failed three times wastes minutes of every warm-up.
        """
        if backend != OPENVINO:
            return weights
        built = artifacts.export("detector", weights, "openvino",
                                 models_dir=self._cfg.models_dir)
        if built.ok and built.path:
            with self._lock:
                self._state.last_export_seconds = built.export_seconds
            if not built.cached:
                log.info("exported detector to OpenVINO IR in %.1fs (%s)",
                         built.export_seconds or 0.0, built.path)
            return built.path
        self.record_failure(backend, f"export failed: {built.error}")
        return None

    # ── outcomes ─────────────────────────────────────────────────────────────
    def record_failure(self, backend: str, error: str) -> None:
        with self._lock:
            self._state.attempts += 1
            self._state.failures.append(f"{backend}: {error}")
            if self._state.attempts >= self._state.max_attempts:
                self._state.exhausted = True
        if self._state.exhausted:
            log.error("%s failed %d time(s) — staying on torch for the life of "
                      "this process. Last error: %s. Restart the container to "
                      "try again, or set ANALYTICS_DETECTOR_BACKEND=torch to stop "
                      "attempting it.", backend, self._state.attempts, error)
        else:
            log.warning("%s failed to load (%s) — using torch for now, will "
                        "retry on the next warm-up (%d of %d attempts used)",
                        backend, error, self._state.attempts,
                        self._state.max_attempts)

    def record_success(self, backend: str) -> None:
        """A load that worked. Deliberately does NOT reset the attempt count.

        A backend that loads intermittently is not a backend that works, and
        zeroing the counter on each success would let one that fails every
        other warm-up retry for ever.
        """
        log.info("detector backend: %s", backend)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_resolved()
            return self._state.to_dict()
