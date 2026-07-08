import os
from pathlib import Path
from typing import Protocol

from ceph_devstack import config, logger
from ceph_devstack.block_pool import BlockPool, BlockPoolError, format_size, parse_size
from ceph_devstack.exec import Subprocess
from ceph_devstack.host import host


class CommandRunner(Protocol):
    async def __call__(
        self,
        args: list[str],
        *,
        check: bool = False,
        force_local: bool = False,
        stream_output: bool = False,
    ) -> Subprocess: ...


class BlockDeviceProvisioner:
    """Create loop devices backed by sparse files or NVMe pool slices."""

    def __init__(
        self,
        owner: str,
        *,
        image_dir: Path,
        file_size: str,
        cmd: CommandRunner,
        trigger_udev: bool = False,
        pool: BlockPool | None = None,
    ):
        self.owner = owner
        self.image_dir = image_dir
        self.file_size = file_size
        self.device_size = parse_size(file_size)
        self.trigger_udev = trigger_udev
        self._cmd = cmd
        self.pool = pool if pool is not None else BlockPool.from_config(config)

    @property
    def pool_enabled(self) -> bool:
        return self.pool is not None

    def _require_pool(self) -> BlockPool:
        assert self.pool is not None
        return self.pool

    def _image_path(self, device: str) -> Path:
        loop_id = device.removeprefix("/dev/loop")
        return self.image_dir / f"{self.owner}-{loop_id}"

    async def create_devices(self, devices: list[str]) -> None:
        for index, device in enumerate(devices):
            await self.create_device(device, index)

    async def remove_devices(self, devices: list[str]) -> None:
        for device in devices:
            await self.remove_device(device)
        if self.pool_enabled:
            self._require_pool().release_owner(self.owner)

    async def create_device(self, device: str, index: int) -> None:
        await self._ensure_loop_module()
        await self.remove_device(device)
        device_pos = device.removeprefix("/dev/loop")
        await self._cmd(
            ["sudo", "mknod", "-m700", device, "b", "7", device_pos],
            check=True,
        )
        await self._cmd(
            ["sudo", "chown", f"{os.getuid()}:{os.getgid()}", device],
            check=True,
        )
        if self.pool_enabled:
            pool = self._require_pool()
            try:
                offset, _size = pool.get_or_allocate(
                    self.owner, index, self.device_size
                )
            except BlockPoolError:
                logger.error(
                    f"{self.owner}: refusing to use block pool parent {pool.parent!r}"
                )
                raise
            logger.info(
                f"{self.owner}: region on {pool.parent} "
                f"(offset={offset}, size={format_size(self.device_size)}) -> {device}"
            )
            await self._cmd(
                [
                    "sudo",
                    "losetup",
                    "--offset",
                    str(offset),
                    "--sizelimit",
                    str(self.device_size),
                    device,
                    pool.parent,
                ],
                check=True,
            )
        else:
            image_path = self._image_path(device)
            os.makedirs(self.image_dir, exist_ok=True)
            await self._cmd(
                [
                    "sudo",
                    "dd",
                    "if=/dev/null",
                    f"of={image_path}",
                    "bs=1",
                    "count=0",
                    f"seek={self.file_size}",
                ],
                check=True,
            )
            await self._cmd(["sudo", "losetup", device, str(image_path)], check=True)

        await self._cmd(["chcon", "-t", "fixed_disk_device_t", device], check=False)
        if self.trigger_udev:
            await self._cmd(
                [
                    "sudo",
                    "udevadm",
                    "trigger",
                    "--action=add",
                    f"--name=block/loop{device_pos}",
                ],
                check=False,
            )
            await self._cmd(["sudo", "udevadm", "settle"], check=False)

    async def remove_device(self, device: str) -> None:
        if os.path.ismount(device):
            await self._cmd(["umount", device], check=True)
        if host.path_exists(device):
            await self._cmd(["sudo", "losetup", "-d", device], check=False)
            await self._cmd(["sudo", "rm", "-f", device], check=False)
        if not self.pool_enabled:
            image_path = self._image_path(device)
            if image_path.exists():
                image_path.unlink()

    async def _ensure_loop_module(self) -> None:
        proc = await self._cmd(["bash", "-c", "lsmod | grep -q loop"], check=False)
        if await proc.wait() != 0:
            await self._cmd(["sudo", "modprobe", "loop"])
