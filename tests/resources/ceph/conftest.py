from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack import config
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner


@pytest.fixture
def pool_config(tmp_path: Path, pool_parent: Path) -> None:
    config["block_pool"] = {
        "parent": str(pool_parent),
        "state_dir": str(tmp_path),
        "allow_enroll": True,
    }
    config["containers"]["ceph_node"]["loop_device_size"] = "1M"
    config["containers"]["testnode"]["loop_device_size"] = "1M"


@pytest.fixture
def pool_provisioner(
    tmp_path: Path,
    pool_parent: Path,
    pool_config: None,
    mock_cmd: AsyncMock,
) -> BlockDeviceProvisioner:
    with patch(
        "ceph_devstack.block_pool.validate_parent_name",
        return_value=str(pool_parent),
    ):
        return BlockDeviceProvisioner(
            "ceph_node",
            image_dir=tmp_path / "disk_images",
            file_size="1M",
            cmd=mock_cmd,
        )


@pytest.fixture
def ready_pool_provisioner(
    pool_provisioner: BlockDeviceProvisioner,
    mock_fresh_enrollment: None,
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
def large_pool_provisioner(
    tmp_path: Path,
    pool_parent: Path,
    pool_config: None,
    mock_cmd: AsyncMock,
) -> BlockDeviceProvisioner:
    with patch(
        "ceph_devstack.block_pool.validate_parent_name",
        return_value=str(pool_parent),
    ):
        return BlockDeviceProvisioner(
            "ceph_node",
            image_dir=tmp_path / "disk_images",
            file_size="5M",
            cmd=mock_cmd,
        )
