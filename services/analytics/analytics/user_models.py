"""user_models.py — operator-uploaded detection models for Data Collection.

Separate from the live detector on purpose: that one is a single global model
tied to running cameras. These are on-demand — an operator uploads a YOLO
`.pt` or `.onnx`, picks classes, and camera-mgmt's collection run calls
`infer()` once per sampled frame to decide whether the frame is worth keeping.

SECURITY: a `.pt` file is a pickle and loading one executes code. The upload
endpoint is only reachable through camera-mgmt, which gates it behind the
`data_collection` capability; prefer `.onnx` for models from outside the team.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import numpy as np

log = logging.getLogger("analytics.user_models")

_ALLOWED = {".pt", ".onnx"}
_ID = re.compile(r"^[0-9a-f]{32}$")


class ModelError(Exception):
    """Operator-facing problem with an uploaded model."""


class UserModelRegistry:
    def __init__(self, root: str | Path, max_loaded: int = 2) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._loaded: dict[str, Any] = {}      # id -> YOLO, small LRU by insertion
        self._max_loaded = max_loaded

    # -- paths ---------------------------------------------------------------
    def _dir(self, model_id: str) -> Path:
        if not _ID.match(model_id or ""):
            raise ModelError("unknown model")
        return self._root / model_id

    def _meta(self, model_id: str) -> dict:
        p = self._dir(model_id) / "meta.json"
        if not p.is_file():
            raise ModelError("unknown model")
        return json.loads(p.read_text())

    # -- CRUD ----------------------------------------------------------------
    def add(self, filename: str, data_path: Path, display_name: Optional[str]) -> dict:
        """Register a file already written to `data_path` (a temp file inside
        the registry root). Loads it once to validate and read class names."""
        ext = Path(filename or "").suffix.lower()
        if ext not in _ALLOWED:
            data_path.unlink(missing_ok=True)
            raise ModelError("model must be a .pt or .onnx file")
        model_id = uuid.uuid4().hex
        d = self._dir(model_id)
        d.mkdir(parents=True)
        weights = d / f"model{ext}"
        data_path.replace(weights)
        try:
            names = self._read_names(weights)
        except Exception as exc:                       # noqa: BLE001
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            d.rmdir()
            raise ModelError(f"could not load model: {exc}") from exc
        if not names:
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            d.rmdir()
            raise ModelError("model has no class names in its metadata")
        meta = {
            "id": model_id,
            "name": (display_name or Path(filename).stem)[:120],
            "filename": Path(filename).name[:200],
            "format": ext.lstrip("."),
            "bytes": weights.stat().st_size,
            "classes": names,
            "created_at": time.time(),
        }
        (d / "meta.json").write_text(json.dumps(meta))
        return meta

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self._root.iterdir()):
            if d.is_dir() and _ID.match(d.name) and (d / "meta.json").is_file():
                try:
                    out.append(json.loads((d / "meta.json").read_text()))
                except ValueError:
                    continue
        out.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        return out

    def get(self, model_id: str) -> dict:
        return self._meta(model_id)

    def delete(self, model_id: str) -> None:
        d = self._dir(model_id)
        self._meta(model_id)
        with self._lock:
            self._loaded.pop(model_id, None)
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()

    # -- inference -----------------------------------------------------------
    @staticmethod
    def _read_names(weights: Path) -> list[str]:
        from ultralytics import YOLO               # late: AGPL seam, heavy import
        m = YOLO(str(weights), task="detect")
        names = m.names
        if isinstance(names, dict):
            return [str(names[k]) for k in sorted(names)]
        return [str(n) for n in names]

    def _model(self, model_id: str):
        with self._lock:
            m = self._loaded.get(model_id)
            if m is not None:
                return m
        meta = self._meta(model_id)
        from ultralytics import YOLO
        m = YOLO(str(self._dir(model_id) / f"model.{meta['format']}"), task="detect")
        with self._lock:
            while len(self._loaded) >= self._max_loaded:
                self._loaded.pop(next(iter(self._loaded)))
            self._loaded[model_id] = m
        return m

    def infer(self, model_id: str, frame_bgr: np.ndarray,
              classes: list[str], conf: float) -> list[dict]:
        """Detections of the wanted classes only. Empty list = nothing found."""
        meta = self._meta(model_id)
        known = meta["classes"]
        wanted_names = {c for c in classes if c in known}
        if classes and not wanted_names:
            raise ModelError("none of the requested classes exist in this model")
        ids = [i for i, n in enumerate(known) if not wanted_names or n in wanted_names]
        model = self._model(model_id)
        with self._lock:                      # one predictor instance is not thread-safe
            r = model(frame_bgr, classes=ids, conf=conf, verbose=False)[0]
        if r.boxes is None or len(r.boxes) == 0:
            return []
        out = []
        for box, cls, cf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(),
                                r.boxes.conf.tolist()):
            out.append({"label": known[int(cls)], "confidence": round(float(cf), 4),
                        "bbox": [round(v, 1) for v in box]})
        return out
