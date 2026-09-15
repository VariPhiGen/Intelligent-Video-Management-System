"""storage/capacity.py — the largest size cap the storage volume can honour.

The size cap limits indexed footage bytes, and until now the only bound on it
was an arbitrary 1,000,000 GB. On a real machine a cap above the volume's
headroom is not a limit at all: ``RetentionManager``'s emergency disk-floor
evictor fires at DISK_FLOOR_FREE_PCT and deletes footage *regardless of the
cap*, so a cap set beyond that point never governs anything — the operator
believes footage is kept to N GB while the disk floor is what actually decides.
This module computes the cap the volume can really honour, so the API can
refuse anything larger and the UI can show the number before it is typed.

The ceiling counts footage already recorded, because that space is reclaimable
by the cap itself::

    max_cap = footage_bytes + free_bytes - (total_bytes * reserve_pct / 100)

Leaving footage out would make a 500 GB cap on a full-but-capped volume report
a maximum near zero. The reserve is the evictor's *recovery* target rather than
its trigger, so a cap set to exactly the maximum still lands above the floor
instead of oscillating on it every health cycle.

Cross-platform by construction: ``shutil.disk_usage`` is ``statvfs`` on Linux
and macOS (``free`` is ``f_bavail``, so root-reserved blocks are already
excluded) and ``GetDiskFreeSpaceExW`` on Windows (free-to-caller, so per-user
quotas are honoured). One call, correct semantics on all three.

Container caveat, surfaced rather than hidden: inside Docker this measures the
filesystem the storage volume actually lives on. Under Docker Desktop
(Windows/macOS) that is the Linux VM's virtual disk, not the host drive, which
is why ``path`` is reported next to the number instead of just a bare size.

That is the right answer only when the VM's disk is FIXED. Docker Desktop on
Windows backs it with a DYNAMICALLY EXPANDING vhdx — a ~1 TB virtual maximum
sitting in a file on a host drive that is usually far smaller. Measured on a
stock install: the container reads 1007 GiB total / 935 GiB free while the
vhdx is a 29 GB file on a C: with 96 GB free. Every number above is then ~10x
the space the host can actually supply, and because the evictor's floor and
the WARNING/CRITICAL meters are all PERCENTAGES of that inflated total, none
of them fire: C: fills completely while the meter reads ~11% used and the
alert state reads "ok". `virtual_backing` detects this and is reported
alongside the sizes so the API, the UI and the operator all see the caveat
instead of a number that cannot be honoured. See `detect_virtual_backing`.
"""

import logging
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from storage.retention import RetentionManager

logger = logging.getLogger(__name__)

_GB = 1024 ** 3

# Headroom kept free for everything on the volume that is not indexed footage:
# extracted clips, the SQLite WAL, grooming temp files, the OS. Deliberately the
# evictor's recovery target and not its trigger — see the module docstring.
RESERVE_FREE_PCT = RetentionManager.DISK_TARGET_FREE_PCT


@dataclass(frozen=True)
class DiskHeadroom:
    """A single probe of the storage volume, in bytes plus the derived cap."""

    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    reserve_bytes: int
    footage_bytes: int
    max_cap_bytes: int
    #: Set when total/free above describe a dynamically expanding virtual disk
    #: whose space the host may not be able to supply — see detect_virtual_backing.
    virtual_backing: str | None = None

    @property
    def max_cap_gb(self) -> int:
        """Floored to whole GB so the advertised maximum is always settable.

        The API takes GB and converts back to bytes; rounding up here would
        advertise a maximum that its own validation then rejects.
        """
        return int(self.max_cap_bytes // _GB)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "reserve_bytes": self.reserve_bytes,
            "reserve_pct": RESERVE_FREE_PCT,
            # None on a normal volume. Non-null means every size here is the
            # VIRTUAL disk's, which the host may not be able to back — consumers
            # must present it as a caveat, not as free space they can rely on.
            "virtual_backing": self.virtual_backing,
        }


# Kernels whose "disk" is a dynamically expanding image file on a host volume we
# cannot see from in here — BOTH Docker Desktop platforms, because the failure
# mode is identical and this module's own header names both:
#   * WSL2 (Docker Desktop for Windows): docker_data.vhdx grows on demand up to
#     a ~1 TB virtual maximum regardless of how little room the host drive has.
#   * LinuxKit (Docker Desktop for macOS): Docker.raw is the same shape — a
#     sparse image with a configurable "virtual disk limit" that can exceed the
#     Mac's actual APFS free space. Matching only the WSL strings left macOS
#     with the exact silent-fill this detection was written to catch.
# (OrbStack also reports a custom kernel but backs storage with real host disk
# via its own filesystem sharing; its /proc/version does not match these.)
_VIRTUAL_KERNEL_MARKERS = {
    "wsl2": ("microsoft-standard-wsl", "-microsoft-standard"),
    "linuxkit": ("-linuxkit",),
}


@lru_cache(maxsize=1)
def detect_virtual_backing() -> str | None:
    """Name the virtualisation whose free space is not backed by real host disk.

    Returns a short identifier ("wsl2", "linuxkit") or None when the
    volume's free space can be taken at face value. One small read of
    /proc/version, which is absent on Windows and macOS hosts running natively.
    Cached: the kernel cannot change under a running process, and the recording
    engine asks on every health cycle.

    Deliberately conservative — it answers "is the reported size possibly
    unbacked?", not "how much room is there really?". The host's true free space
    is genuinely unknowable from inside the VM, so the honest move is to flag
    the number rather than invent a better one. Known coarseness: the check is
    kernel-wide, so a volume bind-mounted from a REAL host drive inside such a
    VM is flagged too — a false caveat costs a warning banner, while the
    inverse (no caveat on an unbacked disk) costs lost footage.
    """
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            version = fh.read().lower()
    except OSError:
        return None
    for name, markers in _VIRTUAL_KERNEL_MARKERS.items():
        if any(m in version for m in markers):
            return name
    return None


def _existing_ancestor(path: Path) -> Path | None:
    """Nearest existing directory at or above `path`.

    The storage path may not exist yet on a first boot, and on Windows an
    operator may point the cap check at ``D:\\vms\\footage`` before creating it.
    Walking up finds the volume, which is all the probe needs. Terminates at the
    filesystem root (``parent == self``), so a missing drive letter yields None
    rather than looping.
    """
    p = path
    while True:
        if p.exists():
            return p
        parent = p.parent
        if parent == p:
            return None
        p = parent


def probe(storage_path, footage_bytes: int = 0,
          reserve_pct: float = RESERVE_FREE_PCT) -> DiskHeadroom | None:
    """Measure the volume behind `storage_path` and derive the maximum cap.

    Returns None when the volume cannot be measured (missing drive, permission
    error, an OS that refuses the stat). Callers **fail open** on None: a cap
    the operator cannot set because a stat failed is a worse outcome than a cap
    that is merely larger than the disk.
    """
    if storage_path is None:
        return None
    resolved = _existing_ancestor(Path(storage_path))
    if resolved is None:
        logger.warning("Capacity probe: no existing path at or above %s", storage_path)
        return None
    try:
        usage = shutil.disk_usage(resolved)
    except OSError as e:
        logger.warning("Capacity probe failed for %s: %s", resolved, e)
        return None

    reserve = int(usage.total * reserve_pct / 100.0)
    footage = max(0, int(footage_bytes))
    return DiskHeadroom(
        path=str(resolved),
        total_bytes=usage.total,
        used_bytes=usage.used,
        free_bytes=usage.free,
        reserve_bytes=reserve,
        footage_bytes=footage,
        # Clamped at zero: a volume already inside its reserve has no room to
        # grant, and a negative maximum would read as an error rather than as
        # "you can only shrink the cap from here".
        max_cap_bytes=max(0, footage + usage.free - reserve),
        virtual_backing=detect_virtual_backing(),
    )


def humanize(num_bytes: int) -> str:
    """Size for an operator-facing message — TB above a terabyte, else GB."""
    tb = num_bytes / (1024 ** 4)
    if tb >= 1:
        return f"{tb:.2f} TB"
    return f"{num_bytes / _GB:.2f} GB"
