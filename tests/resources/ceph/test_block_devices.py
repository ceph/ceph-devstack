from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack.block_pool import BlockPoolError
from ceph_devstack.resources.ceph.block_devices import (
    BlockDeviceProvisioner,
    attached_loops,
)

MiB = 1024**2


class TestBlockDeviceProvisioner:
    async def test_create_device_uses_pool_slice(
        self,
        ready_pool_provisioner: BlockDeviceProvisioner,
        mock_cmd: AsyncMock,
        pool_parent,
    ):
        await ready_pool_provisioner.create_device("/dev/loop9", 0)
        losetup = [
            call.args[0]
            for call in mock_cmd.await_args_list
            if "losetup" in call.args[0]
        ][0]
        offset_idx = losetup.index("--offset")
        assert losetup[offset_idx + 1] == "0"
        sizelimit_idx = losetup.index("--sizelimit")
        assert losetup[sizelimit_idx + 1] == str(ready_pool_provisioner.device_size)
        assert losetup[-2:] == ["/dev/loop9", pool_parent]

    async def test_remove_devices_releases_pool_owner(
        self,
        ready_pool_provisioner: BlockDeviceProvisioner,
    ):
        await ready_pool_provisioner.create_device("/dev/loop9", 0)
        pool = ready_pool_provisioner.pool
        assert pool is not None
        assert list(pool.allocations) == ["ceph_node:0"]
        await ready_pool_provisioner.remove_devices(["/dev/loop9"])
        assert pool.allocations == {}
        assert pool.free_regions == [{"offset": 0, "size": 1024**2}]

    async def test_failed_create_removes_the_device_node(
        self,
        pool_provisioner: BlockDeviceProvisioner,
        mock_cmd: AsyncMock,
    ):
        pool = pool_provisioner.pool
        assert pool is not None
        with (
            patch.object(pool_provisioner, "remove_device", new=AsyncMock()) as remove,
            patch.object(
                pool, "get_or_allocate", side_effect=BlockPoolError("not empty")
            ),
            pytest.raises(BlockPoolError),
        ):
            await pool_provisioner.create_device("/dev/loop9", 0)
        assert ["sudo", "mknod", "-m700", "/dev/loop9", "b", "7", "9"] in [
            call.args[0] for call in mock_cmd.await_args_list
        ]
        assert remove.await_args_list == [(("/dev/loop9",),)] * 2

    async def test_failed_cleanup_does_not_hide_the_create_error(
        self,
        pool_provisioner: BlockDeviceProvisioner,
        caplog,
    ):
        pool = pool_provisioner.pool
        assert pool is not None
        cleanup = AsyncMock(side_effect=[None, RuntimeError("umount failed")])
        with (
            patch.object(pool_provisioner, "remove_device", new=cleanup),
            patch.object(
                pool, "get_or_allocate", side_effect=BlockPoolError("not empty")
            ),
            pytest.raises(BlockPoolError, match="not empty"),
        ):
            await pool_provisioner.create_device("/dev/loop9", 0)
        assert "could not clean up /dev/loop9" in caplog.text

    async def test_remove_devices_warns_about_regions_it_cannot_wipe(
        self,
        ready_pool_provisioner: BlockDeviceProvisioner,
        caplog,
    ):
        pool = ready_pool_provisioner.pool
        assert pool is not None
        with patch.object(pool, "release_owner", return_value=["ceph_node:0"]):
            await ready_pool_provisioner.remove_devices([])
        assert "could not wipe pool regions ceph_node:0" in caplog.text

    async def test_create_device_uses_sparse_file_without_pool(
        self,
        sparse_provisioner: BlockDeviceProvisioner,
        mock_cmd: AsyncMock,
        tmp_path,
    ):
        with (
            patch.object(sparse_provisioner, "remove_device", new=AsyncMock()),
            patch.object(sparse_provisioner, "_ensure_loop_module", new=AsyncMock()),
        ):
            await sparse_provisioner.create_device("/dev/loop1", 0)
        image_dir = tmp_path / "disk_images"
        image_path = str(image_dir / "testnode_0-1")
        commands = [call.args[0] for call in mock_cmd.await_args_list]
        assert ["mkdir", "-p", str(image_dir)] in commands
        dd = next(c for c in commands if "dd" in c)
        assert f"of={image_path}" in dd
        assert f"seek={sparse_provisioner.device_size}" in dd
        assert ["sudo", "losetup", "/dev/loop1", image_path] in commands

    async def test_remove_device_deletes_backing_file_on_host(
        self,
        sparse_provisioner: BlockDeviceProvisioner,
        mock_cmd: AsyncMock,
        tmp_path,
    ):
        await sparse_provisioner.remove_device("/dev/loop1")
        image_path = str(tmp_path / "disk_images" / "testnode_0-1")
        mock_cmd.assert_awaited_once_with(["rm", "-f", image_path], check=False)

    async def test_rejects_region_larger_than_parent(
        self,
        large_pool_provisioner: BlockDeviceProvisioner,
    ):
        with (
            patch.object(large_pool_provisioner, "remove_device", new=AsyncMock()),
            pytest.raises(BlockPoolError, match="exceeds"),
        ):
            await large_pool_provisioner.create_device("/dev/loop9", 0)

    async def test_ensure_loop_module_loads_when_missing(
        self,
        sparse_provisioner: BlockDeviceProvisioner,
        mock_cmd: AsyncMock,
    ):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)
        mock_cmd.return_value = mock_proc
        await sparse_provisioner._ensure_loop_module()
        mock_cmd.assert_any_await(["sudo", "modprobe", "loop"])


class TestHostLoopDiscovery:
    def test_attached_loops_reads_losetup_list(self, fake_host):
        fake_host.responses["losetup"] = (
            0,
            b'{"loopdevices": [{"name": "/dev/loop7", "offset": "4096",'
            b' "back-file": "/dev/sda1"}]}',
        )
        assert attached_loops() == [(7, 4096, "/dev/sda1")]
        assert fake_host.commands == [
            ["losetup", "--list", "--json", "--output", "NAME,OFFSET,BACK-FILE"]
        ]

    def test_pool_owner_devices_are_its_loops_on_its_regions(
        self, fake_host, pool_provisioner
    ):
        pool = pool_provisioner.pool
        assert pool is not None
        mine = [pool.get_or_allocate("ceph_node", i, MiB) for i in (0, 1)]
        other = pool.get_or_allocate("testnode_0", 0, MiB)
        fake_host.loops = [
            (9, mine[1], pool.parent),
            (7, mine[0], pool.parent),
            (3, other, pool.parent),
            (4, mine[0], "/var/lib/other.img"),
        ]
        assert pool_provisioner.owned_devices() == ["/dev/loop7", "/dev/loop9"]

    def test_sparse_owner_devices_come_from_backing_files(
        self, fake_host, sparse_provisioner, tmp_path
    ):
        for name in ("testnode_0-5", "testnode_0-2", "testnode_01-3"):
            fake_host.add_file(tmp_path / "disk_images" / name)
        assert sparse_provisioner.owned_devices() == ["/dev/loop2", "/dev/loop5"]

    async def test_regions_stay_allocated_while_loops_remain_attached(
        self, fake_host, ready_pool_provisioner, mock_cmd, caplog
    ):
        pool = ready_pool_provisioner.pool
        assert pool is not None
        await ready_pool_provisioner.create_device("/dev/loop9", 0)
        fake_host.loops = [(9, 0, pool.parent)]  # detach did not take effect
        with patch("ceph_devstack.resources.ceph.block_devices.asyncio.sleep") as sleep:
            await ready_pool_provisioner.remove_devices(["/dev/loop9"])
        mock_cmd.assert_any_await(["sudo", "udevadm", "settle"], check=False)
        assert sleep.await_count == 4
        assert "/dev/loop9 still attached" in caplog.text
        assert list(pool.allocations) == ["ceph_node:0"]

    async def test_regions_released_once_busy_loop_lets_go(
        self, fake_host, ready_pool_provisioner
    ):
        pool = ready_pool_provisioner.pool
        assert pool is not None
        await ready_pool_provisioner.create_device("/dev/loop9", 0)
        fake_host.loops = [(9, 0, pool.parent)]

        async def detach_later(delay):
            fake_host.loops = []

        with patch(
            "ceph_devstack.resources.ceph.block_devices.asyncio.sleep",
            side_effect=detach_later,
        ):
            await ready_pool_provisioner.remove_devices(["/dev/loop9"])
        assert pool.allocations == {}

    @pytest.mark.parametrize(
        "response",
        [(1, b""), (0, b"not json"), (0, b'{"other": []}')],
        ids=["losetup-fails", "garbage", "unexpected-json"],
    )
    async def test_failed_loop_discovery_aborts_removal(
        self, fake_host, ready_pool_provisioner, response
    ):
        pool = ready_pool_provisioner.pool
        assert pool is not None
        await ready_pool_provisioner.create_device("/dev/loop9", 0)
        fake_host.responses["losetup"] = response
        with pytest.raises(BlockPoolError, match="losetup|loop devices"):
            await ready_pool_provisioner.remove_devices(["/dev/loop9"])
        assert list(pool.allocations) == ["ceph_node:0"]
