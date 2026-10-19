import json
import os
import pathlib
import random
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack import config
from ceph_devstack.block_pool import BlockPool, shared_pool
from ceph_devstack.host import Host


@pytest.fixture(autouse=True)
def reset_config():
    config.load()
    shared_pool.cache_clear()
    yield


class FakeHost(Host):
    """In-memory host for block-pool and loop-device commands.

    ``files`` maps absolute paths to contents; directories are implied by path
    prefixes. ``loops`` lists attached loop devices as (number, offset,
    backing file) for ``losetup --list``. ``responses`` gives (returncode,
    stdout) by argv[0] and overrides everything else; the defaults describe an
    unmounted device without signatures. Any other command fails the test.
    Every command run through ``output`` is recorded in ``commands``.
    """

    def __init__(self):
        self.files: dict[str, bytearray] = {}
        self.loops: list[tuple[int, int, str]] = []
        self.responses: dict[str, tuple[int, bytes]] = {
            "findmnt": (1, b""),
            "blkid": (2, b""),
        }
        self.commands: list[list[str]] = []

    def add_file(self, path: str | pathlib.Path, data: bytes | str = b"") -> None:
        if isinstance(data, str):
            data = data.encode()
        self.files[str(path)] = bytearray(data)

    def add_block_device(
        self, name: str, size: int = 20 * 1024**2, disk: str | None = None
    ) -> str:
        """Add /dev/<name> of size zero bytes and its sysfs entry; return the path.

        For a partition, ``disk`` also lists it under the disk's sysfs directory.
        """
        self.add_file(f"/dev/{name}", bytes(size))
        self.add_file(f"/sys/class/block/{name}/size", f"{size // 512}\n")
        if disk:
            self.add_file(f"/sys/class/block/{disk}/{name}/size", f"{size // 512}\n")
        return f"/dev/{name}"

    def _children(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted(
            {p[len(prefix) :].split("/")[0] for p in self.files if p.startswith(prefix)}
        )

    def path_exists(self, path) -> bool:
        return str(path) in self.files or bool(self._children(str(path)))

    def output(self, args, input=None) -> subprocess.CompletedProcess:
        self.commands.append(args)
        name, *rest = args
        if name in self.responses:
            rc, out = self.responses[name]
        elif name == "cat":
            data = self.files.get(rest[0])
            rc, out = (1, b"") if data is None else (0, bytes(data))
        elif name == "ls":
            children = self._children(rest[-1])
            rc, out = (0, "".join(f"{c}\n" for c in children).encode())
        elif name == "realpath":
            rc, out = 0, f"{rest[0]}\n".encode()
        elif name == "losetup" and rest[0] == "--list":
            loops = [
                {"name": f"/dev/loop{n}", "offset": off, "back-file": back}
                for n, off, back in self.loops
            ]
            rc, out = 0, (json.dumps({"loopdevices": loops}).encode() if loops else b"")
        elif name == "dd":
            opts = dict(arg.split("=", 1) for arg in rest)
            bs = int(opts["bs"])
            rc, out = 0, b""
            if "if" in opts:
                start = int(opts["skip"]) * bs
                out = bytes(
                    self.files[opts["if"]][start : start + int(opts["count"]) * bs]
                )
            else:
                assert input is not None and opts.get("conv") == "notrunc,fsync"
                start = int(opts["seek"]) * bs
                self.files[opts["of"]][start : start + len(input)] = input
        else:
            raise AssertionError(f"unexpected host command {args}")
        return subprocess.CompletedProcess(args, rc, out, b"")


@pytest.fixture(autouse=True)
def fake_host():
    """Isolate block-pool and loop-device code from the real host.

    Nothing reads the real /sys/class/block or block devices; tests that need
    sysfs entries, devices, or backing files add them to the fake.
    """
    fake = FakeHost()
    with (
        patch("ceph_devstack.block_pool.host", fake),
        patch("ceph_devstack.resources.ceph.block_devices.host", fake),
    ):
        yield fake


@pytest.fixture
def mock_cmd() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def pool_parent(fake_host: FakeHost) -> str:
    return fake_host.add_block_device("nvme0n1p1")


@pytest.fixture
def block_pool_factory(
    tmp_path: pathlib.Path, pool_parent: str
) -> Callable[..., BlockPool]:
    def factory(*, allow_enroll: bool = True) -> BlockPool:
        return BlockPool(
            tmp_path / "block_pool.json", pool_parent, allow_enroll=allow_enroll
        )

    return factory


@pytest.fixture
def block_pool(block_pool_factory: Callable[..., BlockPool]) -> BlockPool:
    return block_pool_factory()


@pytest.fixture
def enrolled_pool(block_pool: BlockPool) -> BlockPool:
    block_pool.ensure_ready()
    return block_pool


@pytest.fixture
def disallow_enroll_pool(block_pool_factory: Callable[..., BlockPool]) -> BlockPool:
    return block_pool_factory(allow_enroll=False)


@pytest.fixture(scope="function")
def create_log_file():
    def _create_log_file(data_dir: pathlib.Path, **kwargs) -> pathlib.Path:
        parts = {
            "timestamp": (datetime.now() - timedelta(days=random.randint(1, 100))),
            "test_type": random.choice(["ceph", "rgw", "rbd", "mds"]),
            "job_id": random.randint(1, 100),
            "content": "some log data",
            **kwargs,
        }
        timestamp = parts["timestamp"].strftime("%Y-%m-%d_%H:%M:%S")
        test_type = parts["test_type"]
        job_id = parts["job_id"]
        content = parts["content"]

        run_name = f"root-{timestamp}-orch:cephadm:{test_type}-small-main-distro-default-testnode"
        log_dir = data_dir / "archive" / run_name / str(job_id)

        os.makedirs(log_dir, exist_ok=True)
        time_ = parts["timestamp"].timestamp()
        os.utime(log_dir, times=(time_, time_))
        log_file = log_dir / "teuthology.log"
        log_file.write_text(content)
        os.utime(log_file, times=(time_, time_))
        return log_file

    return _create_log_file
