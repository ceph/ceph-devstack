import contextlib
import fcntl
import functools
import json
import os
import re
import uuid
from pathlib import Path

from ceph_devstack import DEFAULT_DATA_DIR, config
from ceph_devstack.host import host


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$", re.IGNORECASE)
_UNITS = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}
_SECTOR_SIZE = 512
_SYSFS_BLOCK = Path("/sys/class/block")

# Written to the last 4 KiB of an enrolled parent so we can recognize our own
# partitions even if block_pool.json is removed.
_POOL_TAIL_MAGIC = b"CEPH-DEVSTACK-BLOCK-POOL-TAIL-v1\x00"
_TAIL_MARKER_SIZE = 4096
_PROBE_SIZE = 4096

# Whole disks and partitions are allowed; device-mapper/loop types are not.
# NVMe and MMC partitions add "p<N>" to the disk name; SCSI/virtio add "<N>".
_NVME_DISK = r"nvme\d+n\d+"
_MMC_DISK = r"mmcblk\d+"
_SCSI_DISK = r"(?:sd|[xv]d)[a-z]+"
_WHOLE_DISK_NAME = re.compile(rf"^(?:{_NVME_DISK}|{_MMC_DISK}|{_SCSI_DISK})$")
_PARTITION_NAME = re.compile(rf"^(?:(?:{_NVME_DISK}|{_MMC_DISK})p|{_SCSI_DISK})\d+$")
_UNSAFE_NAME = re.compile(r"^(?:dm-\d+|md\d+|loop\d+|ram\d+|fd\d+)$")


class BlockPoolError(ValueError):
    """Raised when a block pool operation would be unsafe."""


def parse_size(value: str | int) -> int:
    text = str(value).strip().upper()
    match = _SIZE_RE.match(text)
    if not match:
        raise BlockPoolError(f"invalid size: {value!r}")
    number, unit = match.groups()
    size = int(float(number) * _UNITS[unit.upper()])
    if size <= 0:
        raise BlockPoolError(f"size must be positive: {value!r}")
    return size


def format_size(size_bytes: int) -> str:
    for unit in ("P", "T", "G", "M", "K"):
        multiplier = _UNITS[unit]
        if size_bytes >= multiplier and size_bytes % multiplier == 0:
            return f"{size_bytes // multiplier}{unit}"
    return f"{size_bytes}B"


def validate_parent_name(parent: str) -> str:
    """Return canonical parent path or raise if the device is not a safe block device."""
    parent = host.realpath(parent)
    if not parent.startswith("/dev/"):
        raise BlockPoolError(f"block pool parent must be a /dev path, not {parent!r}")
    name = os.path.basename(parent)
    if _UNSAFE_NAME.match(name):
        raise BlockPoolError(
            f"block pool parent {parent!r} is not allowed "
            "(device-mapper, md, loop, and ram devices are rejected)"
        )
    if not (_PARTITION_NAME.match(name) or _WHOLE_DISK_NAME.match(name)):
        raise BlockPoolError(
            f"block pool parent {parent!r} must be a block device or partition "
            "(e.g. /dev/nvme0n1, /dev/nvme0n1p1, or /dev/sda1)"
        )
    if not host.path_exists(parent):
        raise BlockPoolError(f"block pool parent {parent!r} does not exist")
    if not host.path_exists(_SYSFS_BLOCK / name):
        raise BlockPoolError(f"block pool parent {parent!r} is not a block device")
    return parent


class BlockPool:
    """Allocate sized regions from a shared parent block device.

    Device access (sysfs, findmnt, blkid, marker and probe I/O) goes through
    the Host abstraction; the state file and its lock stay local.
    """

    @staticmethod
    def _has_partitions(name: str) -> bool:
        """True when a whole-disk parent already has partitions.

        The kernel lists a disk's partitions as subdirectories of its sysfs
        directory, so this needs no name-prefix guessing.
        """
        if not _WHOLE_DISK_NAME.match(name):
            return False
        return any(_PARTITION_NAME.match(e) for e in host.list_dir(_SYSFS_BLOCK / name))

    @functools.cached_property
    def _device_size(self) -> int:
        name = os.path.basename(self.parent)
        sectors = host.read_text(_SYSFS_BLOCK / name / "size")
        try:
            return int(sectors or "") * _SECTOR_SIZE
        except ValueError:
            raise BlockPoolError(f"cannot read size of {self.parent!r}") from None

    def _read_parent(self, offset: int, length: int) -> bytes:
        try:
            return host.read_bytes(self.parent, offset, length)
        except OSError as e:
            raise BlockPoolError(str(e)) from e

    def _write_parent(self, offset: int, data: bytes) -> None:
        try:
            host.write_bytes(self.parent, offset, data)
        except OSError as e:
            raise BlockPoolError(str(e)) from e

    @staticmethod
    def _is_region_empty(data: bytes) -> bool:
        return not data or data == b"\x00" * len(data)

    def _device_has_blkid_signature(self) -> bool:
        proc = host.output(["blkid", "-p", "-o", "export", self.parent])
        if proc.returncode != 0:
            return False
        for line in proc.stdout.decode().splitlines():
            key, _, value = line.partition("=")
            if key in {"TYPE", "PTTYPE"} and value:
                return True
        return False

    def _tail_marker_offset(self) -> int:
        size = self._device_size
        if size < _TAIL_MARKER_SIZE:
            raise BlockPoolError(f"block pool parent {self.parent!r} is too small")
        return size - _TAIL_MARKER_SIZE

    def _read_tail_marker(self) -> dict | None:
        offset = self._tail_marker_offset()
        raw = self._read_parent(offset, _TAIL_MARKER_SIZE)
        if not raw.startswith(_POOL_TAIL_MAGIC):
            return None
        payload = raw[len(_POOL_TAIL_MAGIC) :].split(b"\x00", 1)[0]
        if not payload:
            return None
        try:
            return json.loads(payload.decode())
        except json.JSONDecodeError:
            return None

    def _write_tail_marker(self, payload: dict) -> None:
        offset = self._tail_marker_offset()
        encoded = json.dumps(payload, sort_keys=True).encode()
        if len(_POOL_TAIL_MAGIC) + len(encoded) + 1 > _TAIL_MARKER_SIZE:
            raise BlockPoolError("block pool tail marker payload is too large")
        raw = _POOL_TAIL_MAGIC + encoded + b"\x00"
        raw = raw.ljust(_TAIL_MARKER_SIZE, b"\x00")
        self._write_parent(offset, raw)

    def _probe_parent_empty(self) -> None:
        size = self._device_size
        offsets = [0]
        if size > _SECTOR_SIZE:
            offsets.append(_SECTOR_SIZE)
        if size > _PROBE_SIZE:
            offsets.append(size // 2)
        tail = self._tail_marker_offset()
        if tail not in offsets and tail >= _PROBE_SIZE:
            offsets.append(tail)
        for offset in offsets:
            data = self._read_parent(offset, _PROBE_SIZE)
            if not self._is_region_empty(data):
                raise BlockPoolError(
                    f"block pool parent {self.parent!r} is not empty at offset "
                    f"{offset}; refusing to enroll a partition that may contain data"
                )

    def __init__(
        self,
        state_path: Path,
        parent: str,
        *,
        allow_enroll: bool = False,
    ):
        self.state_path = state_path
        self.parent = validate_parent_name(parent)
        self.allow_enroll = allow_enroll
        self._state = self._load()

    @classmethod
    def from_config(cls, cfg: dict) -> "BlockPool | None":
        pool_cfg = cfg.get("block_pool") or {}
        parent = pool_cfg.get("parent")
        if not parent:
            return None
        state_dir = pool_cfg.get("state_dir") or cfg.get("data_dir", DEFAULT_DATA_DIR)
        state_path = Path(state_dir).expanduser().absolute() / "block_pool.json"
        allow_enroll = pool_cfg.get("allow_enroll", False) is True
        return cls(state_path, parent, allow_enroll=allow_enroll)

    @property
    def enrolled(self) -> bool:
        return bool(self._state.get("enrolled"))

    @property
    def pool_id(self) -> str:
        return self._state["pool_id"]

    @property
    def allocations(self) -> dict[str, dict]:
        """Allocated regions keyed by "<owner>:<index>"."""
        return dict(self._state["allocations"])

    def owner_offsets(self, owner: str) -> set[int]:
        """Offsets of the regions currently allocated to owner (fresh from disk)."""
        with self._state_lock():
            return {
                region["offset"]
                for region in self._state["allocations"].values()
                if region["owner"] == owner
            }

    @property
    def free_regions(self) -> list[dict]:
        return list(self._state["free_regions"])

    @property
    def next_offset(self) -> int:
        return self._state["next_offset"]

    def _load(self) -> dict:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if state.get("parent") != self.parent:
                raise BlockPoolError(
                    f"block pool parent changed ({state.get('parent')!r} -> "
                    f"{self.parent!r}); remove {self.state_path} to reconfigure"
                )
            return state
        return self._fresh_state()

    def _fresh_state(self) -> dict:
        return {
            "parent": self.parent,
            "pool_id": str(uuid.uuid4()),
            "enrolled": False,
            "next_offset": 0,
            "free_regions": [],
            "allocations": {},
        }

    @contextlib.contextmanager
    def _state_lock(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(".lock")
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if self.state_path.exists():
                    self._state = self._load()
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _atomic_save(self) -> None:
        tmp_path = self.state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp_path, self.state_path)

    def ensure_ready(self) -> None:
        """Verify the parent partition is safe to use before any slice I/O."""
        with self._state_lock():
            if self._ensure_ready_unlocked():
                self._atomic_save()

    def _ensure_ready_unlocked(self) -> bool:
        if self.enrolled:
            self._verify_enrolled_parent()
            return False
        self._enroll_new_parent()
        return True

    def _verify_enrolled_parent(self) -> None:
        marker = self._read_tail_marker()
        if marker is None:
            raise BlockPoolError(
                f"block pool parent {self.parent!r} is missing the devstack "
                f"enrollment marker; refusing to use it. If {self.state_path} "
                "was removed accidentally, start again to reclaim the marker. "
                "If you wiped the device, set block_pool.allow_enroll = true "
                "for a fresh enrollment."
            )
        if marker.get("pool_id") != self._state.get("pool_id"):
            raise BlockPoolError(
                f"block pool parent {self.parent!r} belongs to another pool "
                f"({marker.get('pool_id')!r}); remove {self.state_path} only "
                "if you intentionally reset the pool"
            )
        if marker.get("parent") != self.parent:
            raise BlockPoolError(
                f"block pool parent path mismatch for enrolled partition "
                f"({marker.get('parent')!r} != {self.parent!r})"
            )

    def _enroll_new_parent(self) -> None:
        marker = self._read_tail_marker()
        if marker is not None:
            if marker.get("parent") != self.parent:
                raise BlockPoolError(
                    f"block pool parent {self.parent!r} has a devstack marker "
                    f"for a different path ({marker.get('parent')!r})"
                )
            self._state["pool_id"] = marker.get("pool_id")
            self._state["enrolled"] = True
            return

        if not self.allow_enroll:
            raise BlockPoolError(
                f"block pool parent {self.parent!r} is not enrolled. "
                "Use a dedicated empty partition and set "
                "block_pool.allow_enroll = true for the first run only."
            )
        if host.device_mounted(self.parent):
            raise BlockPoolError(
                f"block pool parent {self.parent!r} is mounted; refusing to enroll"
            )
        if self._device_has_blkid_signature():
            raise BlockPoolError(
                f"block pool parent {self.parent!r} has a filesystem or partition "
                "table signature; refusing to enroll"
            )

        if self._has_partitions(os.path.basename(self.parent)):
            raise BlockPoolError(
                f"block pool parent {self.parent!r} is a whole disk with "
                "existing partitions; use a dedicated empty disk or a single "
                "partition instead"
            )
        self._probe_parent_empty()

        payload = {
            "pool_id": self._state["pool_id"],
            "parent": self.parent,
        }
        self._write_tail_marker(payload)
        self._state["enrolled"] = True

    def _validate_region(self, offset: int, size: int, owner: str) -> None:
        parent_size = self._device_size
        if offset + size > parent_size:
            raise BlockPoolError(
                f"block pool region for {owner!r} at offset {offset} with size "
                f"{size} exceeds parent size {parent_size} for {self.parent!r}"
            )
        if offset >= self._tail_marker_offset():
            raise BlockPoolError(
                f"block pool region for {owner!r} would overlap the enrollment "
                f"marker on {self.parent!r}"
            )
        probe = min(_PROBE_SIZE, size)
        data = self._read_parent(offset, probe)
        if not self._is_region_empty(data):
            raise BlockPoolError(
                f"block pool region for {owner!r} on {self.parent!r} is not empty "
                f"at offset {offset}; refusing to allocate over existing data"
            )

    def _pop_free_region(self, size: int) -> dict | None:
        free_regions = self._state["free_regions"]
        for index, region in enumerate(free_regions):
            if region["size"] == size:
                return free_regions.pop(index)
        return None

    def get_or_allocate(self, owner: str, index: int, size: int) -> int:
        """Offset of the region for owner's index-th device, allocating if new."""
        with self._state_lock():
            changed = self._ensure_ready_unlocked()
            key = f"{owner}:{index}"
            allocations = self._state["allocations"]
            if key in allocations:
                if changed:
                    self._atomic_save()
                return allocations[key]["offset"]

            if region := self._pop_free_region(size):
                offset = region["offset"]
            else:
                offset = self._state["next_offset"]
                self._state["next_offset"] = offset + size

            self._validate_region(offset, size, owner)
            allocations[key] = {"offset": offset, "size": size, "owner": owner}
            self._atomic_save()
            return offset

    def release_owner(self, owner: str) -> list[str]:
        """Return owner's regions to the free list; list the ones kept back.

        Each region's probe area is zeroed first, so that _validate_region
        accepts it when it is handed out again; anything else found there
        later is foreign data and still refused. A region that cannot be
        zeroed stays allocated to owner (releasing again retries it), and
        its "<owner>:<index>" key is returned. Nothing is written, and every
        region is kept, unless the parent still carries this pool's
        enrollment marker: the device name may now point at another disk.
        """
        kept = []
        with self._state_lock():
            prefix = f"{owner}:"
            allocations = self._state["allocations"]
            free_regions = self._state["free_regions"]
            keys = [key for key in allocations if key.startswith(prefix)]
            if not self.enrolled:
                return keys
            try:
                self._verify_enrolled_parent()
            except BlockPoolError:
                return keys
            for key in keys:
                region = allocations[key]
                probe = min(_PROBE_SIZE, region["size"])
                try:
                    self._write_parent(region["offset"], b"\x00" * probe)
                except BlockPoolError:
                    kept.append(key)
                    continue
                del allocations[key]
                free_regions.append(
                    {"offset": region["offset"], "size": region["size"]}
                )
            free_regions.sort(key=lambda region: region["offset"])
            self._atomic_save()
        return kept


@functools.cache
def shared_pool() -> BlockPool | None:
    """The block pool for the loaded config, built once per process and shared.

    State is re-read under the lock for every operation, so one instance can
    serve every requirement check and provisioner in a run.
    """
    return BlockPool.from_config(config)
