from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack.block_pool import BlockPoolError
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner


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
        assert "--offset" in losetup
        assert "0" in losetup
        assert "--sizelimit" in losetup
        assert str(ready_pool_provisioner.device_size) in losetup
        assert str(pool_parent) in losetup

    async def test_remove_devices_releases_pool_owner(
        self,
        ready_pool_provisioner: BlockDeviceProvisioner,
    ):
        await ready_pool_provisioner.create_device("/dev/loop9", 0)
        await ready_pool_provisioner.remove_devices(["/dev/loop9"])
        assert (
            ready_pool_provisioner._require_pool().allocation_for("ceph_node", 0)
            is None
        )

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
        dd_calls = [
            call.args[0] for call in mock_cmd.await_args_list if "dd" in call.args[0]
        ]
        assert dd_calls
        image_path = str(tmp_path / "disk_images" / "testnode_0-1")
        assert any(image_path in arg for arg in dd_calls[0])

    async def test_rejects_region_larger_than_parent(
        self,
        large_pool_provisioner: BlockDeviceProvisioner,
        mock_fresh_enrollment: None,
    ):
        with (
            patch(
                "ceph_devstack.block_pool._device_size_bytes",
                return_value=512 * 1024,
            ),
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

    async def test_pool_error_is_logged_and_reraised(
        self,
        pool_provisioner: BlockDeviceProvisioner,
        mock_fresh_enrollment: None,
    ):
        with (
            patch.object(
                pool_provisioner._require_pool(),
                "get_or_allocate",
                side_effect=BlockPoolError("refused"),
            ),
            patch.object(pool_provisioner, "remove_device", new=AsyncMock()),
            pytest.raises(BlockPoolError, match="refused"),
        ):
            await pool_provisioner.create_device("/dev/loop9", 0)
