from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack import config
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner


@pytest.fixture
def ceph_node_config() -> Callable[..., dict]:
    """Return a factory that overrides the shipped ceph_node config."""

    def _configure(**overrides) -> dict:
        config["containers"]["ceph_node"].update(overrides)
        return config["containers"]["ceph_node"]

    return _configure


@pytest.fixture
def mock_loop_devices():
    """Patch LoopDeviceMixin.allocate_loop_devices to return three devices."""
    with patch(
        "ceph_devstack.resources.ceph.host_loops.LoopDeviceMixin.allocate_loop_devices",
        return_value=["/dev/loop0", "/dev/loop1", "/dev/loop2"],
    ) as mock:
        yield mock


@pytest.fixture
def pool_provisioner_factory(tmp_path: Path, block_pool, mock_cmd: AsyncMock):
    def factory(file_size: str = "1M") -> BlockDeviceProvisioner:
        return BlockDeviceProvisioner(
            "ceph_node",
            image_dir=tmp_path / "disk_images",
            file_size=file_size,
            cmd=mock_cmd,
            pool=block_pool,
        )

    return factory


@pytest.fixture
def pool_provisioner(pool_provisioner_factory) -> BlockDeviceProvisioner:
    return pool_provisioner_factory()


@pytest.fixture
def ready_pool_provisioner(
    pool_provisioner: BlockDeviceProvisioner,
) -> Iterator[BlockDeviceProvisioner]:
    with patch.object(pool_provisioner, "remove_device", new=AsyncMock()):
        yield pool_provisioner


@pytest.fixture
def sparse_provisioner(tmp_path: Path, mock_cmd: AsyncMock) -> BlockDeviceProvisioner:
    return BlockDeviceProvisioner(
        "testnode_0",
        image_dir=tmp_path / "disk_images",
        file_size="1M",
        cmd=mock_cmd,
        pool=None,
    )


@pytest.fixture
def large_pool_provisioner(pool_provisioner_factory) -> BlockDeviceProvisioner:
    return pool_provisioner_factory(file_size="32M")
