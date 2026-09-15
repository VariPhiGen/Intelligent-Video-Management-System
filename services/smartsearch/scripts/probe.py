#!/usr/bin/env python3
"""probe — report what this image can actually run inference on.

WHY THIS EXISTS AS A SCRIPT. Three facts about the deployed environment decide
which backend candidates are real, and none of them can be answered from a
development machine or from documentation:

  1. What device / execution-provider choices the INSTALLED fast_plate_ocr
     exposes. Its constructor takes `device=`, but whether that reaches an
     onnxruntime provider list is a property of the version installed here.

  2. Whether an ultralytics ONNX or OpenVINO export preserves `model.names`.
     UltralyticsPlateLocaliser reads those names to decide which classes are
     plates (index/plates.py). If an export drops them the filter silently
     becomes class-agnostic and whole trucks are handed to the recogniser —
     which decodes them as empty, and CONFIDENTLY, so the cost is invisible
     and the symptom is "ANPR does nothing".

  3. Whether onnxruntime-openvino can coexist with the plain onnxruntime that
     fast-plate-ocr[onnx] already installs. They provide the same module.

The answers differ between the CPU image and the cu121 image, so run it in
both. It reads the environment and writes nothing outside a temporary export
directory it removes afterwards.

    python3 scripts/probe.py                  # human-readable report
    python3 scripts/probe.py --json           # machine-readable, for a profile
    python3 scripts/probe.py --quick          # skip export + model construction
    python3 scripts/probe.py --weights a.pt   # probe operator plate weights too

Deep checks need packages that may not be installed yet — an absence is a
finding, and the report says exactly which one is missing rather than failing.
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from index import hardware  # noqa: E402

#: Detector weights ultralytics downloads on first use. Probing with the same
#: file the service uses keeps the finding about THIS model, not a stand-in.
DEFAULT_WEIGHTS = os.environ.get("SEARCH_DETECTOR_WEIGHTS", "yolov8n.pt")
#: fast-plate-ocr's shipped recognition model, matching plates.model.
DEFAULT_OCR_MODEL = os.environ.get("SEARCH_PLATE_OCR_MODEL", "cct-xs-v2-global-model")


# ── question 1: what does the installed fast_plate_ocr accept? ───────────────
def probe_ocr(model_name: str, construct: bool) -> dict[str, Any]:
    """Signature and accepted device values of LicensePlateRecognizer.

    Introspection first, construction second. The signature says what the API
    OFFERS; only constructing says what this machine ACCEPTS — a CUDA value can
    be a valid argument and still fail because the installed runtime has no
    CUDA provider. Both are reported, because they answer different questions.
    """
    out: dict[str, Any] = {"available": False}
    try:
        mod = importlib.import_module("fast_plate_ocr")
    except Exception as exc:                                       # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["available"] = True
    out["version"] = getattr(mod, "__version__", "unknown")
    cls = getattr(mod, "LicensePlateRecognizer", None)
    if cls is None:
        out["error"] = "LicensePlateRecognizer not exported by this version"
        return out

    try:
        sig = inspect.signature(cls.__init__)
        out["init_signature"] = str(sig)
        out["init_parameters"] = {
            name: {
                "annotation": "" if p.annotation is inspect.Parameter.empty
                              else str(p.annotation),
                "default": None if p.default is inspect.Parameter.empty
                           else repr(p.default),
            }
            for name, p in sig.parameters.items() if name != "self"
        }
        # The decisive question for the candidate matrix: is provider choice
        # exposed at all, or only the coarse device switch?
        out["exposes_provider_choice"] = any(
            k in out["init_parameters"]
            for k in ("providers", "onnx_provider", "provider", "execution_provider",
                      "session_options", "provider_options")
        )
    except (TypeError, ValueError) as exc:
        out["error"] = f"could not introspect: {exc}"

    if not construct:
        out["construction"] = "skipped (--quick)"
        return out

    # Downloads ~3 MB on first run, into the models volume.
    results: dict[str, Any] = {}
    for device in ("auto", "cpu", "cuda"):
        started = time.monotonic()
        try:
            cls(model_name, device=device)
            results[device] = {"ok": True,
                               "load_seconds": round(time.monotonic() - started, 2)}
        except Exception as exc:                                   # noqa: BLE001
            results[device] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out["construction"] = results
    return out


# ── candidate compatibility: does it initialise, run, and answer sanely? ─────
def registered_camera_urls(api: str = "http://127.0.0.1:8013",
                           limit: int = 4) -> list[str]:
    """Relay URLs for the cameras this service is indexing right now.

    THE BEST BENCHMARK INPUT A DEPLOYMENT HAS. Aspect ratio is not cosmetic
    here — measured 2026-09-04, the same backend pair differed by 1.5x on a
    portrait frame and 2.9x on a wide one, so a candidate ranked on a bundled
    square-ish photograph can be ranked wrong for the cameras actually
    installed. Only cameras currently SAMPLING are offered: a reconnecting one
    would block the open and spend the budget on a timeout.
    """
    import urllib.request

    try:
        with urllib.request.urlopen(f"{api}/cameras", timeout=5) as fh:
            payload = json.load(fh)
    except Exception as exc:                                       # noqa: BLE001
        log_line = f"could not read the camera registry at {api}: {exc}"
        print(f"  (note) {log_line}", file=sys.stderr)
        return []
    out = []
    for cam in payload.get("cameras", []):
        if cam.get("state") == "SAMPLING" and cam.get("rtsp_url"):
            out.append(cam["rtsp_url"])
        if len(out) >= limit:
            break
    return out


def _sample_frame():
    """A real image if one is to hand, synthetic noise otherwise.

    AVAILABILITY IS NOT COMPATIBILITY. An importable runtime that produces a
    well-formed empty result on noise has proved almost nothing; the same
    runtime agreeing with the torch reference on a picture containing actual
    people and vehicles has proved the export is faithful enough to benchmark.
    ultralytics bundles `bus.jpg`, which holds several persons and a bus — both
    inside this service's COCO class filter — so where it exists it is a far
    better probe input than anything synthesised here.

    LIVE CAMERA FRAMES COME FIRST when this runs inside a service that has
    any. They carry the deployment's real resolution, aspect ratio and
    compression, and a candidate ranked without them can be ranked wrong — see
    registered_camera_urls. Only then the bundled photographs (real people and
    vehicles, but not these cameras).

    The last fallback is deterministic noise, and the report says which was
    used. A compatibility verdict reached on noise is reported as weak evidence
    rather than dressed up as agreement.
    """
    import numpy as np
    try:
        from index import frames as frames_mod
        urls = registered_camera_urls()
        if urls:
            fs = frames_mod.from_camera(urls[0], count=1)
            if fs is not None and fs.frames:
                f = fs.frames[0]
                return f, f"live camera ({f.shape[1]}x{f.shape[0]})"
    except Exception:                                              # noqa: BLE001
        pass
    try:
        from ultralytics.utils import ASSETS
        import cv2
        for name in ("bus.jpg", "zidane.jpg"):
            candidate = os.path.join(str(ASSETS), name)
            if os.path.exists(candidate):
                img = cv2.imread(candidate)
                if img is not None:
                    return img, f"ultralytics assets/{name}"
    except Exception:                                              # noqa: BLE001
        pass
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (640, 640, 3), dtype="uint8"), "synthetic noise"


def _detections_agree(a: list, b: list, tol: int = 8) -> bool:
    """Same objects, same classes, boxes within `tol` pixels.

    Deliberately tolerant on geometry and strict on membership. Downstream, a
    few pixels of box jitter is absorbed by the min-crop filter and the
    deduplicator; a MISSED detection is not absorbed by anything and becomes a
    search that returns nothing, which is the one answer this product must
    never give wrongly.
    """
    if len(a) != len(b):
        return False
    for da, db in zip(sorted(a, key=lambda d: d.xyxy),
                      sorted(b, key=lambda d: d.xyxy)):
        if da.domain != db.domain:
            return False
        if any(abs(x - y) > tol for x, y in zip(da.xyxy, db.xyxy)):
            return False
    return True


def _run_candidate(artifact: str, frame, device: str, reference=None) -> dict[str, Any]:
    """Initialise, execute and validate one artifact through the REAL interface.

    Goes through index.detector.UltralyticsDetector rather than calling the
    model directly, because that class owns the preprocessing, the class filter
    and the box decoding. A candidate benchmarked without them would be
    measuring something the service never runs.
    """
    from index.detector import UltralyticsDetector

    entry: dict[str, Any] = {"artifact": artifact, "device": device}
    try:
        started = time.monotonic()
        det = UltralyticsDetector(artifact, confidence=0.35, device=device)
        entry["init_seconds"] = round(time.monotonic() - started, 2)
        entry["initialises"] = True
    except Exception as exc:                                       # noqa: BLE001
        entry["initialises"] = False
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return entry

    try:
        det.detect(frame)                                  # warm-up, discarded
        started = time.monotonic()
        found = list(det.detect(frame))
        entry["executes"] = True
        entry["latency_seconds"] = round(time.monotonic() - started, 3)
        entry["detections"] = len(found)
        entry["domains"] = sorted({d.domain for d in found})
        # Output validity: a well-formed detection has a positive-area box
        # inside the frame and a confidence in [0, 1].
        h, w = frame.shape[:2]
        entry["outputs_valid"] = all(
            0 <= d.xyxy[0] < d.xyxy[2] <= w and 0 <= d.xyxy[1] < d.xyxy[3] <= h
            and 0.0 <= d.confidence <= 1.0 for d in found
        )
        entry["backend"] = det.backend.to_dict()
        if reference is not None:
            entry["agrees_with_torch"] = _detections_agree(reference, found)
        entry["_detections"] = found
    except Exception as exc:                                       # noqa: BLE001
        entry["executes"] = False
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


# ── question 2: does an export keep the class names? ─────────────────────────
def probe_export(weights: str, formats: tuple[str, ...],
                 functional: bool = True) -> dict[str, Any]:
    """Export `weights` to each format, reload it, and compare class names.

    THE COMPARISON IS THE POINT, not whether the export succeeds. A localiser
    whose names survive can keep filtering to plate classes; one whose names are
    lost cannot, and that candidate has to be dropped rather than shipped with a
    filter that quietly matches everything.

    The detector is unaffected either way — it filters on fixed COCO ids — so a
    negative result here costs the localiser candidates only.
    """
    out: dict[str, Any] = {"weights": weights}
    try:
        from ultralytics import YOLO
    except Exception as exc:                                       # noqa: BLE001
        out["error"] = f"ultralytics unavailable: {exc}"
        return out

    if not os.path.exists(weights) and not weights.endswith(".pt"):
        out["error"] = f"weights not found: {weights}"
        return out

    try:
        reference = YOLO(weights)
        ref_names = dict(getattr(reference, "names", {}) or {})
        out["reference_names"] = ref_names
        out["reference_class_count"] = len(ref_names)
    except Exception as exc:                                       # noqa: BLE001
        out["error"] = f"could not load reference weights: {type(exc).__name__}: {exc}"
        return out

    # The torch incumbent is the reference every exported candidate is compared
    # against. There is no labelled ground truth on an appliance, and "does this
    # agree with what the site already runs" is the question that matters anyway.
    frame = source = reference = None
    if functional:
        frame, source = _sample_frame()
        out["probe_input"] = source
        out["probe_input_is_real"] = not source.startswith("synthetic")
        ref = _run_candidate(weights, frame, "cpu")
        reference = ref.pop("_detections", None)
        out["torch_reference"] = ref

    per_format: dict[str, Any] = {}
    workdir = tempfile.mkdtemp(prefix="smartsearch-probe-")
    try:
        for fmt in formats:
            entry: dict[str, Any] = {}
            started = time.monotonic()
            try:
                # Export into a scratch copy so the service's own weights
                # directory is never mutated by a probe.
                staged = os.path.join(workdir, os.path.basename(weights))
                if os.path.exists(weights):
                    shutil.copy2(weights, staged)
                else:                       # ultralytics will fetch it by name
                    staged = weights
                exported = YOLO(staged).export(format=fmt, verbose=False)
                entry["export_seconds"] = round(time.monotonic() - started, 1)
                entry["artifact"] = str(exported)
                entry["exists"] = os.path.exists(str(exported))

                reloaded = YOLO(str(exported))
                got = dict(getattr(reloaded, "names", {}) or {})
                entry["names"] = got
                entry["class_count"] = len(got)
                # Compare by value, not identity: exported metadata may key
                # classes as strings where torch keys them as ints.
                entry["names_preserved"] = (
                    sorted(str(v) for v in got.values())
                    == sorted(str(v) for v in ref_names.values())
                )
                entry["ok"] = True
                if functional and frame is not None:
                    run = _run_candidate(str(exported), frame, "cpu", reference)
                    run.pop("_detections", None)
                    entry["functional"] = run
            except Exception as exc:                               # noqa: BLE001
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
            per_format[fmt] = entry
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    out["formats"] = per_format
    return out


# ── question 3: can the onnxruntime distributions coexist? ───────────────────
def probe_onnxruntime_packages() -> dict[str, Any]:
    """Which onnxruntime distributions are installed, and what they provide.

    More than one installed at once is the failure mode: they all provide the
    `onnxruntime` module, so the last one installed wins and the others are
    shadowed. That is exactly the fragile setup to avoid rather than manage.
    """
    out: dict[str, Any] = {}
    try:
        from importlib import metadata
        found = {}
        for dist in metadata.distributions():
            name = (dist.metadata.get("Name") or "").lower()
            if name.startswith("onnxruntime"):
                found[name] = dist.version
        out["installed_distributions"] = found
        out["conflict"] = len(found) > 1
    except Exception as exc:                                       # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"

    try:
        import onnxruntime as ort
        out["module_version"] = ort.__version__
        out["available_providers"] = list(ort.get_available_providers())
        out["module_file"] = getattr(ort, "__file__", None)
    except Exception as exc:                                       # noqa: BLE001
        out["module_error"] = f"{type(exc).__name__}: {exc}"
    return out


# ── report ───────────────────────────────────────────────────────────────────
def _flag(ok: Optional[bool]) -> str:
    return {True: "yes", False: "NO", None: "?"}[ok]


def render(report: dict[str, Any]) -> str:
    hw = report["hardware"]
    cpu, gpu = hw["cpu"], hw["gpu"]
    L: list[str] = []
    add = L.append

    add("=" * 72)
    add("SMART SEARCH BACKEND PROBE")
    add("=" * 72)

    add("")
    add("HARDWARE")
    add(f"  CPU            {cpu['model']}")
    add(f"  architecture   {cpu['arch']}   ISA class: {cpu['isa']}")
    add(f"  processors     host {cpu['host_cpus']}, usable {cpu['usable_cpus']}"
        + ("   <-- CONSTRAINED by cgroup/affinity" if cpu["constrained"] else ""))
    if gpu["present"]:
        add(f"  GPU            {gpu['name']}  ({gpu['vram_mb']} MB, "
            f"capability {gpu['capability']}, x{gpu['count']})")
        add(f"  CUDA runtime   {gpu['cuda_runtime']}   driver: {gpu['driver'] or 'unknown (pynvml absent)'}")
    else:
        add(f"  GPU            none detected"
            + (f"   ({gpu['error']})" if gpu.get("error") else ""))
    add(f"  default device {hw['resolved_default_device']}")

    add("")
    add("RUNTIMES")
    for name, info in hw["runtimes"].items():
        state = f"{info['version']}" if info["available"] else "NOT INSTALLED"
        add(f"  {name:<16} {state}")
        if info.get("providers"):
            add(f"                   providers: {', '.join(info['providers'])}")
        if info.get("devices"):
            add(f"                   devices:   {', '.join(info['devices'])}")
        if not info["available"] and info.get("error"):
            add(f"                   {info['error']}")

    add("")
    add("Q1  fast_plate_ocr device / provider surface")
    ocr = report["ocr"]
    if not ocr.get("available"):
        add(f"  UNAVAILABLE -- {ocr.get('error')}")
    else:
        add(f"  version                 {ocr.get('version')}")
        add(f"  __init__                {ocr.get('init_signature', '?')}")
        add(f"  exposes provider choice {_flag(ocr.get('exposes_provider_choice'))}")
        con = ocr.get("construction")
        if isinstance(con, dict):
            for device, res in con.items():
                if res.get("ok"):
                    add(f"  device={device:<6}          OK ({res['load_seconds']}s)")
                else:
                    add(f"  device={device:<6}          FAILED -- {res['error']}")
        else:
            add(f"  construction            {con}")

    add("")
    add("Q2  ultralytics export: class names, and does the candidate actually run")
    for label, exp in report["exports"].items():
        add(f"  [{label}]  {exp.get('weights')}")
        if exp.get("error"):
            add(f"      {exp['error']}")
            continue
        add(f"      reference classes: {exp.get('reference_class_count')} "
            f"{list(exp.get('reference_names', {}).values())[:6]}")
        if exp.get("probe_input"):
            add(f"      probe input:       {exp['probe_input']}"
                + ("" if exp.get("probe_input_is_real")
                   else "   <-- WEAK evidence: agreement on noise proves little"))
        ref = exp.get("torch_reference") or {}
        if ref:
            if ref.get("executes"):
                add(f"      torch reference:   {ref.get('detections')} detections "
                    f"{ref.get('domains')}  {ref.get('latency_seconds')}s/frame")
            else:
                add(f"      torch reference:   FAILED -- {ref.get('error')}")
        for fmt, entry in (exp.get("formats") or {}).items():
            if not entry.get("ok"):
                add(f"      {fmt:<10} EXPORT FAILED -- {entry.get('error')}")
                continue
            add(f"      {fmt:<10} names preserved: {_flag(entry.get('names_preserved'))}"
                f"   classes: {entry.get('class_count')}"
                f"   export: {entry.get('export_seconds')}s")
            fn = entry.get("functional")
            if not fn:
                continue
            if not fn.get("initialises"):
                add(f"                 CANNOT INITIALISE -- {fn.get('error')}")
            elif not fn.get("executes"):
                add(f"                 CANNOT EXECUTE -- {fn.get('error')}")
            else:
                add(f"                 runs: {fn.get('detections')} detections "
                    f"{fn.get('domains')}   {fn.get('latency_seconds')}s/frame"
                    f"   valid: {_flag(fn.get('outputs_valid'))}"
                    f"   agrees with torch: {_flag(fn.get('agrees_with_torch'))}")

    add("")
    add("Q3  onnxruntime distribution coexistence")
    ort = report["onnxruntime"]
    installed = ort.get("installed_distributions") or {}
    if installed:
        for name, ver in installed.items():
            add(f"  installed      {name} {ver}")
    else:
        add("  installed      (none found)")
    add(f"  conflict       {_flag(ort.get('conflict'))}"
        + ("   <-- more than one distribution provides `onnxruntime`"
           if ort.get("conflict") else ""))
    if ort.get("available_providers"):
        add(f"  providers      {', '.join(ort['available_providers'])}")
    if ort.get("module_error"):
        add(f"  import error   {ort['module_error']}")

    add("")
    add("=" * 72)
    missing = [n for n, i in hw["runtimes"].items() if not i["available"]]
    if missing:
        add("Deep checks were limited by missing packages: " + ", ".join(missing))
        add("To answer the export question, a throwaway install is enough:")
        add("    pip install openvino onnx onnxslim && python3 scripts/probe.py")
    add("=" * 72)
    return "\n".join(L)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Smart Search backend capability probe")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quick", action="store_true",
                    help="skip model construction and export checks")
    ap.add_argument("--weights", default=DEFAULT_WEIGHTS,
                    help=f"detector weights to export-test (default {DEFAULT_WEIGHTS})")
    ap.add_argument("--plate-weights", default=os.environ.get("SEARCH_PLATE_WEIGHTS", ""),
                    help="operator plate-localiser weights; the class-name check "
                         "that actually matters is this one")
    ap.add_argument("--ocr-model", default=DEFAULT_OCR_MODEL)
    args = ap.parse_args(argv)

    formats = ("onnx", "openvino")
    exports: dict[str, Any] = {}
    if args.quick:
        exports["detector"] = {"weights": args.weights, "error": "skipped (--quick)"}
    else:
        exports["detector"] = probe_export(args.weights, formats)
        if args.plate_weights:
            exports["plate_localiser"] = probe_export(args.plate_weights, formats)

    report = {
        "probe_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hardware": hardware.snapshot(),
        "fingerprint": hardware.fingerprint({"detector": args.weights}).to_dict(),
        "ocr": probe_ocr(args.ocr_model, construct=not args.quick),
        "exports": exports,
        "onnxruntime": probe_onnxruntime_packages(),
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
