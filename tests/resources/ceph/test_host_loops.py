from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceph_devstack import config
from ceph_devstack.block_pool import BlockPoolError
from ceph_devstack.resources.ceph.block_devices import (
    BlockDeviceProvisioner,
    loop_number,
    loop_path,
    stack_image_dirs,
)
from ceph_devstack.resources.ceph.host_loops import LoopDeviceMixin


def make_backing(fake_host, image_dir, *names):
    """Create empty backing files for loop devices on the (fake) host."""
    for name in names:
        fake_host.add_file(image_dir / name)


def make_node(name, image_dir, count=2):
    class Node(LoopDeviceMixin):
        loop_img_dir = image_dir
        cmd = AsyncMock()

        def __init__(self):
            self.name = name
            self.config = {"loop_device_count": count, "loop_device_size": "1M"}
            self._init_loop_devices()

    return Node()


class TestHostLoops:
    def test_loop_path_round_trips(self):
        assert loop_path(21) == "/dev/loop21"
        assert loop_number(loop_path(21)) == 21

    def test_discover_used_loop_numbers_from_backing_files(self, fake_host, tmp_path):
        make_backing(fake_host, tmp_path, "testnode_0-1", "ceph_node-4", "ignore-me")
        assert LoopDeviceMixin.discover_used_loop_numbers(tmp_path) == {1, 4}

    def test_discover_used_loop_numbers_across_stacks(self, fake_host, tmp_path):
        config["data_dir"] = str(tmp_path)
        make_backing(fake_host, tmp_path / "teuthology/disk_images", "testnode_0-1")
        make_backing(fake_host, tmp_path / "ceph/disk_images", "ceph_node-2")
        make_backing(fake_host, tmp_path / "disk_images", "testnode_1-3")  # pre-stack
        image_dir = tmp_path / "ceph/disk_images"
        assert LoopDeviceMixin.discover_used_loop_numbers(image_dir) == {1, 2, 3}

    def test_stack_image_dirs_takes_one_host_command(self, fake_host, tmp_path):
        fake_host.add_file(tmp_path / "block_pool.json")
        make_backing(fake_host, tmp_path / "teuthology/disk_images", "testnode_0-1")
        make_backing(fake_host, tmp_path / "ceph/cluster", "ceph.conf")
        fake_host.commands.clear()
        assert stack_image_dirs(tmp_path) == [
            tmp_path / "disk_images",
            tmp_path / "teuthology/disk_images",
        ]
        assert [c[0] for c in fake_host.commands] == ["find"]

    def test_discover_used_loop_numbers_includes_attached_loops(
        self, fake_host, tmp_path
    ):
        fake_host.loops = [(0, 0, "/img"), (5, 1048576, "/dev/nvme0n1p1")]
        make_backing(fake_host, tmp_path, "testnode_0-3")
        assert LoopDeviceMixin.discover_used_loop_numbers(tmp_path) == {0, 3, 5}

    def test_allocation_fails_when_attached_loops_are_unknown(
        self, fake_host, tmp_path
    ):
        fake_host.responses["losetup"] = (1, b"")
        with pytest.raises(BlockPoolError, match="cannot list loop devices"):
            LoopDeviceMixin.allocate_loop_numbers("testnode_0", 1, tmp_path)

    def test_owner_loop_numbers(self, fake_host, tmp_path):
        make_backing(
            fake_host, tmp_path, "testnode_1-2", "testnode_1-0", "testnode_10-1"
        )
        assert LoopDeviceMixin.owner_loop_numbers("testnode_1", tmp_path) == [0, 2]

    def test_allocate_loop_numbers_uses_lowest_free(self, fake_host, tmp_path):
        make_backing(fake_host, tmp_path, "other-0")
        assert LoopDeviceMixin.allocate_loop_numbers("testnode_0", 2, tmp_path) == [
            1,
            2,
        ]

    def test_allocate_loop_numbers_reuses_owner_backing_files(
        self, fake_host, tmp_path
    ):
        make_backing(fake_host, tmp_path, "testnode_1-4", "testnode_1-1", "other-0")
        assert LoopDeviceMixin.allocate_loop_numbers("testnode_1", 3, tmp_path) == [
            1,
            4,
            2,
        ]

    async def test_remove_targets_held_devices_without_allocating(
        self, fake_host, tmp_path
    ):
        """A fresh process (e.g. `remove`) acts on what the host shows it holds."""
        make_backing(fake_host, tmp_path, "testnode_0-4", "other-0")
        node = make_node("testnode_0", tmp_path)
        with (
            patch.object(LoopDeviceMixin, "allocate_loop_devices") as allocate,
            patch.object(BlockDeviceProvisioner, "remove_devices") as remove,
        ):
            await node.remove_loop_devices()
        allocate.assert_not_called()
        remove.assert_awaited_once_with(["/dev/loop4"])

    def test_allocate_loop_numbers_avoids_cross_call_collisions(
        self, fake_host, tmp_path
    ):
        """Multiple owners allocating from the same dir get distinct numbers."""
        make_backing(fake_host, tmp_path, "existing-0")
        a = LoopDeviceMixin.allocate_loop_numbers("testnode_0", 1, tmp_path)
        b = LoopDeviceMixin.allocate_loop_numbers("testnode_1", 1, tmp_path)
        c = LoopDeviceMixin.allocate_loop_numbers("testnode_2", 1, tmp_path)
        assert a == [1]
        assert b == [2]
        assert c == [3]

    async def test_removed_devices_are_released_for_reallocation(self, tmp_path):
        first = make_node("testnode_0", tmp_path)
        assert first.allocate_devices() == ["/dev/loop0", "/dev/loop1"]
        provisioner = MagicMock(
            owned_devices=MagicMock(return_value=[]), remove_devices=AsyncMock()
        )
        with patch.object(first, "_block_provisioner", return_value=provisioner):
            await first.remove_loop_devices()
        provisioner.remove_devices.assert_awaited_once_with(
            ["/dev/loop0", "/dev/loop1"]
        )
        second = make_node("testnode_1", tmp_path)
        assert second.allocate_devices() == ["/dev/loop0", "/dev/loop1"]
