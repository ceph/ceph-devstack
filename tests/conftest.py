import os
import pathlib
import random
from collections.abc import Callable
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack import config
from ceph_devstack.block_pool import BlockPool


@pytest.fixture(autouse=True)
def reset_config():
    config.load()
    yield


@pytest.fixture
def mock_cmd() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def pool_parent(tmp_path: pathlib.Path) -> pathlib.Path:
    parent = tmp_path / "nvme0n1p1"
    parent.write_bytes(b"\x00" * (20 * 1024**2))
    return parent


@pytest.fixture
def mock_fresh_enrollment():
    with (
        patch("ceph_devstack.block_pool._device_mounted", return_value=False),
        patch(
            "ceph_devstack.block_pool._device_has_blkid_signature", return_value=False
        ),
        patch("ceph_devstack.block_pool._read_tail_marker", return_value=None),
    ):
        yield


@pytest.fixture
def block_pool_factory(
    tmp_path: pathlib.Path, pool_parent: pathlib.Path
) -> Callable[..., BlockPool]:
    def factory(*, enrolled: bool = False, allow_enroll: bool = True) -> BlockPool:
        state_path = tmp_path / "block_pool.json"
        with patch(
            "ceph_devstack.block_pool.validate_parent_name",
            return_value=str(pool_parent),
        ):
            pool = BlockPool(
                state_path,
                str(pool_parent),
                allow_enroll=allow_enroll,
            )
        if enrolled:
            pool._state["enrolled"] = True
            pool._save()
        return pool

    return factory


@pytest.fixture
def block_pool(block_pool_factory: Callable[..., BlockPool]) -> BlockPool:
    return block_pool_factory()


@pytest.fixture
def enrolled_pool(block_pool_factory: Callable[..., BlockPool]) -> BlockPool:
    return block_pool_factory(enrolled=True)


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
