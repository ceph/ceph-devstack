from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack import DEFAULT_CEPH_IMAGE, config
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner


TEUTHOLOGY_SERVICES = [
    "postgres",
    "paddles",
    "beanstalk",
    "pulpito",
    "teuthology",
    "testnode",
    "archive",
    "package_repo",
    "registry",
]


@pytest.fixture
def builder_config(tmp_path: Path) -> Callable[..., dict]:
    """Return a factory that installs a minimal ceph_builder config.

    Call it with keyword overrides to customise individual keys.
    """

    def _configure(**overrides) -> dict:
        base = {
            "image_builder": "binary-patch",
            "repo": str(tmp_path),
            "sccache": False,
            "npm_cache": False,
        }
        base.update(overrides)
        config["containers"]["ceph_builder"] = base
        return config["containers"]["ceph_builder"]

    return _configure


@pytest.fixture
def ceph_node_config() -> Callable[..., dict]:
    """Return a factory that installs a minimal ceph_node config."""

    def _configure(**overrides) -> dict:
        base = {
            "image_builder": "binary-patch",
            "loop_device_count": 3,
            "loop_device_size": "5G",
            "image": DEFAULT_CEPH_IMAGE,
        }
        base.update(overrides)
        config["containers"]["ceph_node"] = base
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
def mock_git_identity():
    """Patch git_sha1 and git_branch in resources.utils."""
    with (
        patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123"),
        patch("ceph_devstack.resources.utils.git_branch", return_value="main"),
    ):
        yield


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
    file_backed_device_size,
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
    file_backed_device_size,
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
