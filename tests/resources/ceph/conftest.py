from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack import config
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner


@pytest.fixture
def teuthology_services() -> list[str]:
    """The shipped teuthology stack's services."""
    return list(config["stacks"]["teuthology"]["services"])


@pytest.fixture
def mock_git() -> Iterator[None]:
    """Make every repo look like it is at sha1 abc123 on branch main."""
    with (
        patch(
            "ceph_devstack.resources.ceph.ceph_builder.git_sha1", return_value="abc123"
        ),
        patch(
            "ceph_devstack.resources.ceph.ceph_builder.git_branch", return_value="main"
        ),
        patch(
            "ceph_devstack.resources.ceph.containers.git_sha1", return_value="abc123"
        ),
    ):
        yield


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
