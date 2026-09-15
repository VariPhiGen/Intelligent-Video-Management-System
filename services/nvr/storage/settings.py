"""storage/settings.py — runtime-editable, restart-surviving global NVR settings.

The only globally-tunable knob today is the storage size cap. It is seeded from
``NVR_MAX_STORAGE_GB`` (via main.py) on first boot, then persisted to a small
JSON file in the writable data dir so a value set from the UI survives an NVR
restart — the env var becomes just the default when no persisted value exists.

The NVR is otherwise stateless (camera-mgmt's DB is the source of truth for
per-camera settings); this is deliberately the one small exception, scoped to a
single file that the retention loop and the ``/storage`` API both read live.
"""

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class StorageSettings:
    """Thread-safe holder for the global storage size cap, persisted to disk.

    Written by the API (async loop), read live by the retention thread each
    pass. A ``threading.Lock`` guards the write + file replace; reads of the
    single int attribute are atomic under the GIL.
    """

    def __init__(self, path, default_max_storage_bytes: int | None = None):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._max_storage_bytes = default_max_storage_bytes
        self._load()

    def _load(self) -> None:
        """Persisted value wins over the seeded default when the file exists."""
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return  # first boot — keep the env/YAML default
        except (OSError, ValueError):
            logger.exception(
                "StorageSettings: %s unreadable — keeping default", self._path)
            return
        if isinstance(data, dict) and "max_storage_bytes" in data:
            v = data["max_storage_bytes"]
            self._max_storage_bytes = int(v) if v else None
            logger.info(
                "StorageSettings: loaded persisted size cap = %s bytes",
                self._max_storage_bytes)

    @property
    def max_storage_bytes(self) -> int | None:
        return self._max_storage_bytes

    def get_max_storage_bytes(self) -> int | None:
        """Callable form for RetentionManager's live size-limit provider hook."""
        return self._max_storage_bytes

    def set_max_storage_bytes(self, value: int | None) -> None:
        """Update the cap and persist it atomically. ``None`` = uncapped."""
        with self._lock:
            self._max_storage_bytes = value
            self._save()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"max_storage_bytes": self._max_storage_bytes}))
        tmp.replace(self._path)  # atomic on POSIX
        logger.info(
            "StorageSettings: persisted size cap = %s bytes",
            self._max_storage_bytes)
