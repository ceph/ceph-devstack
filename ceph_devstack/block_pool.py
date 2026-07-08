import contextlib
import fcntl
import json
import os
import re
import subprocess
import uuid
from pathlib import Path

from ceph_devstack import logger


_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$", re.IGNORECASE)
_UNITS = {
    "": 1,
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}

# Written to the last 4 KiB of an enrolled parent so we can recognize our own
# partitions even if block_pool.json is removed.
_POOL_TAIL_MAGIC = b"CEPH-DEVSTACK-BLOCK-POOL-TAIL-v1\x00"
_TAIL_MARKER_SIZE = 4096
_PROBE_SIZE = 4096

# Whole disks and partitions are allowed; device-mapper/loop types are not.
_WHOLE_DISK_NAME = re.compile(
    r"^(?:"
    r"nvme\d+n\d+|"
    r"mmcblk\d+|"
    r"(?:sd|[xv]d)[a-z]+"
    r")$"
)
_PARTITION_NAME = re.compile(
    r"^(?:"
    r"nvme\d+n\d+p\d+|"
    r"mmcblk\d+p\d+|"
    r"(?:sd|[xv]d)[a-z]+\d+"
    r")$"
)
_UNSAFE_NAME = re.compile(r"^(?:dm-\d+|md\d+|loop\d+|ram\d+|fd\d+)$")


class BlockPoolError(ValueError):
    """Raised when a block pool operation would be unsafe."""


def parse_size(value: str | int) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().upper()
    match = _SIZE_RE.match(text)
    if not match:
        raise BlockPoolError(f"invalid size: {value!r}")
    number, unit = match.groups()
    return int(float(number) * _UNITS[unit.upper()])


def format_size(size_bytes: int) -> str:
    for unit, multiplier in (
        ("P", 1024**5),
        ("T", 1024**4),
        ("G", 1024**3),
        ("M", 1024**2),
        ("K", 1024),
    ):
        if size_bytes >= multiplier and size_bytes % multiplier == 0:
            return f"{size_bytes // multiplier}{unit}"
    return f"{size_bytes}B"


def canonical_device_path(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def validate_parent_name(parent: str) -> str:
    """Return canonical parent path or raise if the device is not a safe block device."""
    parent = canonical_device_path(parent)
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
    if not os.path.exists(parent):
        raise BlockPoolError(f"block pool parent {parent!r} does not exist")
    if not os.path.exists(f"/sys/class/block/{name}"):
        raise BlockPoolError(f"block pool parent {parent!r} is not a block device")
    return parent


def _is_whole_disk(name: str) -> bool:
    return bool(_WHOLE_DISK_NAME.match(name))


def _has_partition_siblings(name: str) -> bool:
    """True when a whole-disk parent already has partition devices present."""
    if not _is_whole_disk(name):
        return False
    block_dir = Path("/sys/class/block")
    if not block_dir.is_dir():
        return False
    for entry in block_dir.iterdir():
        if not entry.is_dir() or entry.name == name:
            continue
        if name.startswith("nvme") or name.startswith("mmcblk"):
            if entry.name.startswith(f"{name}p"):
                return True
        elif re.match(r"^(?:sd|[xv]d)[a-z]+$", name):
            suffix = entry.name.removeprefix(name)
            if suffix.isdigit():
                return True
    return False


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _device_size_bytes(parent: str) -> int:
    name = os.path.basename(parent)
    sectors_path = f"/sys/class/block/{name}/size"
    try:
        sectors = int(Path(sectors_path).read_text().strip())
        return sectors * 512
    except OSError:
        pass
    try:
        return os.path.getsize(parent)
    except OSError as exc:
        raise BlockPoolError(f"cannot read size of {parent!r}: {exc}") from exc


def _read_device(parent: str, offset: int, length: int) -> bytes:
    with open(parent, "rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def _write_device(parent: str, offset: int, data: bytes) -> None:
    with open(parent, "r+b") as handle:
        handle.seek(offset)
        handle.write(data)


def _is_region_empty(data: bytes) -> bool:
    return not data or data == b"\x00" * len(data)


def _device_mounted(parent: str) -> bool:
    proc = _run(["findmnt", "-n", "-o", "TARGET", "--source", parent])
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _device_has_blkid_signature(parent: str) -> bool:
    proc = _run(["blkid", "-p", "-o", "export", parent])
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in {"TYPE", "PTTYPE"} and value:
            return True
    return False


def _tail_marker_offset(parent: str) -> int:
    size = _device_size_bytes(parent)
    if size < _TAIL_MARKER_SIZE:
        raise BlockPoolError(f"block pool parent {parent!r} is too small")
    return size - _TAIL_MARKER_SIZE


def _read_tail_marker(parent: str) -> dict | None:
    offset = _tail_marker_offset(parent)
    raw = _read_device(parent, offset, _TAIL_MARKER_SIZE)
    if not raw.startswith(_POOL_TAIL_MAGIC):
        return None
    payload = raw[len(_POOL_TAIL_MAGIC) :].split(b"\x00", 1)[0]
    if not payload:
        return None
    try:
        return json.loads(payload.decode())
    except json.JSONDecodeError:
        return None


def _write_tail_marker(parent: str, payload: dict) -> None:
    offset = _tail_marker_offset(parent)
    encoded = json.dumps(payload, sort_keys=True).encode()
    if len(_POOL_TAIL_MAGIC) + len(encoded) + 1 > _TAIL_MARKER_SIZE:
        raise BlockPoolError("block pool tail marker payload is too large")
    raw = _POOL_TAIL_MAGIC + encoded + b"\x00"
    raw = raw.ljust(_TAIL_MARKER_SIZE, b"\x00")
    _write_device(parent, offset, raw)


def _probe_parent_empty(parent: str) -> None:
    size = _device_size_bytes(parent)
    offsets = [0]
    if size > 512:
        offsets.append(512)
    if size > _PROBE_SIZE:
        offsets.append(size // 2)
    tail = _tail_marker_offset(parent)
    if tail not in offsets and tail >= _PROBE_SIZE:
        offsets.append(tail)
    for offset in offsets:
        data = _read_device(parent, offset, _PROBE_SIZE)
        if not _is_region_empty(data):
            raise BlockPoolError(
                f"block pool parent {parent!r} is not empty at offset {offset}; "
                "refusing to enroll a partition that may contain data"
            )


class BlockPool:
    """Allocate sized regions from a shared parent block device."""

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
        state_dir = Path(
            os.path.expanduser(
                pool_cfg.get("state_dir", "~/.local/share/ceph-devstack")
            )
        )
        state_path = state_dir / "block_pool.json"
        allow_enroll = pool_cfg.get("allow_enroll", False) is True
        return cls(state_path, parent, allow_enroll=allow_enroll)

    @classmethod
    def status_from_config(cls, cfg: dict) -> int:
        pool = cls.from_config(cfg)
        if pool is None:
            logger.info("block pool: disabled (no parent configured)")
            return 0
        logger.info(f"block pool parent: {pool.parent}")
        logger.info(f"enrolled: {pool.enrolled}")
        logger.info(f"pool id: {pool._state.get('pool_id')}")
        logger.info(f"state file: {pool.state_path}")
        allocations = pool._state.get("allocations", {})
        if allocations:
            logger.info("allocations:")
            for key, region in sorted(allocations.items()):
                owner = region.get("owner", "?")
                logger.info(
                    f"  {key}: offset {region['offset']}, "
                    f"size {format_size(region['size'])}, last owner {owner}"
                )
        else:
            logger.info("allocations: (none)")
        free_regions = pool._state.get("free_regions", [])
        if free_regions:
            logger.info("free regions:")
            for region in free_regions:
                logger.info(
                    f"  offset {region['offset']}, size {format_size(region['size'])}"
                )
        else:
            logger.info("free regions: (none)")
        logger.info(f"next offset: {pool._state.get('next_offset', 0)}")
        return 0

    @property
    def enabled(self) -> bool:
        return bool(self.parent)

    @property
    def enrolled(self) -> bool:
        return bool(self._state.get("enrolled"))

    def _load(self) -> dict:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text())
            if state.get("parent") != self.parent:
                raise BlockPoolError(
                    f"block pool parent changed ({state.get('parent')!r} -> "
                    f"{self.parent!r}); remove {self.state_path} to reconfigure"
                )
            if "slice_size" in state:
                raise BlockPoolError(
                    f"legacy block pool state at {self.state_path} uses slice_size; "
                    "remove it to migrate to per-device allocations"
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

    def _save(self) -> None:
        self._atomic_save()

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
        marker = _read_tail_marker(self.parent)
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
        marker = _read_tail_marker(self.parent)
        if marker is not None:
            if marker.get("parent") != self.parent:
                raise BlockPoolError(
                    f"block pool parent {self.parent!r} has a devstack marker "
                    f"for a different path ({marker.get('parent')!r})"
                )
            marker_pool_id = marker.get("pool_id")
            if marker_pool_id != self._state.get("pool_id"):
                self._state["pool_id"] = marker_pool_id
            self._state["enrolled"] = True
            return

        if not self.allow_enroll:
            raise BlockPoolError(
                f"block pool parent {self.parent!r} is not enrolled. "
                "Use a dedicated empty partition and set "
                "block_pool.allow_enroll = true for the first run only."
            )
        if _device_mounted(self.parent):
            raise BlockPoolError(
                f"block pool parent {self.parent!r} is mounted; refusing to enroll"
            )
        if _device_has_blkid_signature(self.parent):
            raise BlockPoolError(
                f"block pool parent {self.parent!r} has a filesystem or partition "
                "table signature; refusing to enroll"
            )

        parent_name = os.path.basename(self.parent)
        if _is_whole_disk(parent_name) and _has_partition_siblings(parent_name):
            raise BlockPoolError(
                f"block pool parent {self.parent!r} is a whole disk with "
                "existing partitions; use a dedicated empty disk or a single "
                "partition instead"
            )
        _probe_parent_empty(self.parent)

        payload = {
            "pool_id": self._state["pool_id"],
            "parent": self.parent,
        }
        _write_tail_marker(self.parent, payload)
        self._state["enrolled"] = True

    def _validate_region(self, offset: int, size: int, owner: str) -> None:
        parent_size = _device_size_bytes(self.parent)
        if offset + size > parent_size:
            raise BlockPoolError(
                f"block pool region for {owner!r} at offset {offset} with size "
                f"{size} exceeds parent size {parent_size} for {self.parent!r}"
            )
        if offset >= _tail_marker_offset(self.parent):
            raise BlockPoolError(
                f"block pool region for {owner!r} would overlap the enrollment "
                f"marker on {self.parent!r}"
            )
        probe = min(_PROBE_SIZE, size)
        data = _read_device(self.parent, offset, probe)
        if not _is_region_empty(data):
            raise BlockPoolError(
                f"block pool region for {owner!r} on {self.parent!r} is not empty "
                f"at offset {offset}; refusing to allocate over existing data"
            )

    def _pop_free_region(self, size: int) -> dict | None:
        free_regions = self._state.setdefault("free_regions", [])
        for index, region in enumerate(free_regions):
            if region["size"] == size:
                return free_regions.pop(index)
        return None

    def get_or_allocate(self, owner: str, index: int, size: int) -> tuple[int, int]:
        with self._state_lock():
            changed = self._ensure_ready_unlocked()
            key = f"{owner}:{index}"
            allocations = self._state.setdefault("allocations", {})
            if key in allocations:
                region = allocations[key]
                if changed:
                    self._atomic_save()
                return region["offset"], region["size"]

            if region := self._pop_free_region(size):
                offset = region["offset"]
            else:
                offset = self._state.setdefault("next_offset", 0)
                self._state["next_offset"] = offset + size

            self._validate_region(offset, size, owner)
            allocations[key] = {"offset": offset, "size": size, "owner": owner}
            self._atomic_save()
            return offset, size

    def release_owner(self, owner: str) -> None:
        with self._state_lock():
            prefix = f"{owner}:"
            allocations = self._state.get("allocations", {})
            free_regions = self._state.setdefault("free_regions", [])
            released = []
            for key, region in list(allocations.items()):
                if key.startswith(prefix):
                    released.append(
                        {"offset": region["offset"], "size": region["size"]}
                    )
                    del allocations[key]
            free_regions.extend(released)
            free_regions.sort(key=lambda region: region["offset"])
            self._state["free_regions"] = free_regions
            self._atomic_save()

    def allocation_for(self, owner: str, index: int) -> tuple[int, int, int] | None:
        key = f"{owner}:{index}"
        region = self._state.get("allocations", {}).get(key)
        if region is None:
            return None
        return region["offset"], region["size"], region["size"]
