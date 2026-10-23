from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner


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
