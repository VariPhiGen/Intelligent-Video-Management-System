"""embedder.py — one CLIP model, batched, shared by every camera.

ONE INSTANCE. The retiring clip-service learned this the expensive way: each
restart reloads the model and costs ~4 minutes of downtime, so anything that
loads a second copy per camera does not scale past a handful. This class is
constructed once and every camera's crops flow through it.

The same model must serve both sides of the search: image vectors at ingest and
text vectors at query time. They are only comparable because they come from one
encoder — which is also why changing the model is a full re-index rather than a
migration.

WHY THERE IS AN INTERFACE HERE BUT ONLY ONE IMPLEMENTATION. Every other model
in this service can have its backend chosen by calibration; this one cannot, and
the reason is the corpus rather than the export. A different backend produces a
slightly different vector, and the rows already stored were produced by this
one — so a swap creates a permanently mixed index, queried by whichever text
encoder is current. Proving that safe means showing RANK agreement against the
live index, not vector closeness: the same top-k, in the same order, for real
queries. That test needs a mature index and real queries, and the moment a
backend would be chosen is first calibration, when a deployment has neither.

So the interface exists and the candidate list has one entry. That is a
deliberate state, not an unfinished one: the seam means adding a proven
alternative later is a registry entry rather than a refactor of the two callers
that hold an encoder (index/pipeline.py and index/queries.py).
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol, Sequence

import numpy as np
from PIL import Image

from .backends import BY_SOLE, BackendSpec, torch_spec
from .hardware import resolve_device

log = logging.getLogger("smartsearch.embedder")


class Embedder(Protocol):
    """Both sides of the search, from one model.

    `dim` is on the interface because the schema is `vector(512)` and a model
    of another width is not a configuration difference, it is an incompatible
    index — index/models.py refuses one permanently rather than retrying.
    """
    dim: int

    def embed_images(self, crops: Sequence[Image.Image]) -> np.ndarray: ...
    def embed_text(self, query: str) -> np.ndarray: ...

    @property
    def device(self) -> str: ...

    @property
    def backend(self) -> BackendSpec: ...


class ClipEmbedder:
    def __init__(self, model_name: str = "ViT-B-32",
                 pretrained: str = "laion2b_s34b_b79k",
                 device: str | None = None, batch_size: int = 32) -> None:
        import open_clip
        import torch

        self._torch = torch
        self._device = resolve_device(device)
        self._batch = batch_size
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained
        )
        self._model = model.to(self._device).eval()
        self._preprocess = preprocess
        self._tokenizer = open_clip.get_tokenizer(model_name)
        self.dim = int(self._model.visual.output_dim)
        # One model, one GPU stream: serialise access rather than discovering
        # thread-safety the hard way under load.
        self._lock = threading.Lock()
        # BY_SOLE, not BY_INCUMBENT: the other components report "incumbent"
        # because alternatives exist but are unproven. Here the single entry is
        # the decision — see the note at the top of this file.
        self._backend = torch_spec(
            "embedder", self._device,
            artifact=f"{model_name}/{pretrained}", selected_by=BY_SOLE,
        )
        log.info("embedder loaded %s/%s on %s (dim=%d)",
                 model_name, pretrained, self._device, self.dim)

    @property
    def device(self) -> str:
        return self._device

    @property
    def backend(self) -> BackendSpec:
        return self._backend

    def embed_images(self, crops: Sequence[Image.Image]) -> np.ndarray:
        """L2-normalised image vectors, (n, dim). Empty input -> (0, dim)."""
        if not crops:
            return np.zeros((0, self.dim), dtype=np.float32)
        out: list[np.ndarray] = []
        with self._lock, self._torch.no_grad():
            for i in range(0, len(crops), self._batch):
                batch = self._torch.stack(
                    [self._preprocess(c) for c in crops[i:i + self._batch]]
                ).to(self._device)
                v = self._model.encode_image(batch)
                v = v / v.norm(dim=-1, keepdim=True)
                out.append(v.cpu().numpy().astype(np.float32))
        return np.vstack(out)

    def embed_text(self, query: str) -> np.ndarray:
        with self._lock, self._torch.no_grad():
            t = self._tokenizer([query]).to(self._device)
            v = self._model.encode_text(t)
            v = v / v.norm(dim=-1, keepdim=True)
            return v.cpu().numpy().astype(np.float32)[0]
