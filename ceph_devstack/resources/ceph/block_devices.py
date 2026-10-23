import asyncio
import json
from pathlib import Path
from typing import Protocol

from ceph_devstack import logger
from ceph_devstack.block_pool import BlockPool, BlockPoolError, format_size, parse_size
from ceph_devstack.exec import Subprocess
from ceph_devstack.host import host

# After detaching, wait up to _DETACH_CHECKS * _DETACH_RETRY_DELAY seconds for
# busy loops (e.g. still held by udev) to let go of their pool regions.
_DETACH_CHECKS = 5
_DETACH_RETRY_DELAY = 0.5


def backing_name(owner: str, loop_number: int) -> str:
    """Backing file name for a loop device owned by a container."""
    return f"{owner}-{loop_number}"


def parse_backing_name(name: str) -> tuple[str, int] | None:
    """Parse backing file name into (owner, loop_number) or return None."""
    owner, sep, loop_id = name.rpartition("-")
    if not sep or not loop_id.isdigit():
        return None
    return owner, int(loop_id)


def loop_path(number: int) -> str:
    """Host /dev/loop<N> path for a loop number."""
    return f"/dev/loop{number}"


def loop_number(path: str) -> int:
    """Extract loop number from /dev/loop<N> path."""
    return int(path.removeprefix("/dev/loop"))


def attached_loops() -> list[tuple[int, int, str]]:
    """(loop number, offset, backing file) of every attached loop on the host.

    One ``losetup --list`` call reads all of it from sysfs; no root needed.
    Raises BlockPoolError if the loops can't be listed: callers decide what is
    free or safe to release from this, so a failure must never look empty.
    """
    proc = host.output(
        ["losetup", "--list", "--json", "--output", "NAME,OFFSET,BACK-FILE"]
    )
    if proc.returncode != 0:
        raise BlockPoolError(
            f"cannot list loop devices: {proc.stderr.decode().strip()}"
        )
    if not proc.stdout.strip():
        return []  # losetup prints nothing when no loops are attached
    try:
        return [
            (loop_number(loop["name"]), int(loop["offset"]), loop["back-file"])
            for loop in json.loads(proc.stdout)["loopdevices"]
        ]
    except (ValueError, KeyError, TypeError) as e:
        raise BlockPoolError(f"cannot parse losetup output: {e}") from e


def backing_files(image_dir: Path) -> list[tuple[str, int]]:
    """(owner, loop number) for each backing file in image_dir on the host."""
    return [
        parsed
        for name in host.list_dir(image_dir)
        if (parsed := parse_backing_name(name)) is not None
    ]


def stack_image_dirs(base: Path) -> list[Path]:
    """Every stack's disk_images directory, plus the pre-stack one in base.

    One host command finds them, so other entries in base (block_pool.json,
    stacks without disk images) cost no further host round trips.
    """
    proc = host.output(
        [
            "find",
            str(base),
            "-mindepth",
            "2",
            "-maxdepth",
            "2",
            "-type",
            "d",
            "-name",
            "disk_images",
        ]
    )
    found = proc.stdout.decode().splitlines() if proc.returncode == 0 else []
    return [base / "disk_images", *sorted(Path(line) for line in found if line)]


class CommandRunner(Protocol):
    async def __call__(
        self,
        args: list[str],
        *,
        check: bool = False,
    ) -> Subprocess: ...


class BlockDeviceProvisioner:
    """Create loop devices backed by sparse files or NVMe pool slices.

    All commands, including backing-file management in image_dir, run on the
    container host: losetup resolves image paths there.
    """

    def __init__(
        self,
        owner: str,
        *,
        image_dir: Path,
        file_size: str,
        cmd: CommandRunner,
        trigger_udev: bool = False,
        pool: BlockPool | None,
    ):
        self.owner = owner
        self.image_dir = Path(image_dir).expanduser().absolute()
        self.device_size = parse_size(file_size)
        self.trigger_udev = trigger_udev
        self._cmd = cmd
        self.pool = pool

    def _image_path(self, device: str) -> Path:
        num = loop_number(device)
        return self.image_dir / backing_name(self.owner, num)

    async def create_devices(self, devices: list[str]) -> None:
        for index, device in enumerate(devices):
            await self.create_device(device, index)

    def owned_devices(self) -> list[str]:
        """Loop devices this owner holds on the host, found from host state.

        Pool mode: loops attached to the pool parent at one of the owner's
        region offsets. Sparse mode: loops named by the owner's backing files.
        """
        if (pool := self.pool) is not None:
            offsets = pool.owner_offsets(self.owner)
            numbers = [
                number
                for number, offset, backing in attached_loops()
                if backing == pool.parent and offset in offsets
            ]
        else:
            numbers = [
                number
                for owner, number in backing_files(self.image_dir)
                if owner == self.owner
            ]
        return [loop_path(number) for number in sorted(numbers)]

    async def remove_devices(self, devices: list[str]) -> None:
        """Detach devices; then release the owner's pool regions once detached.

        If loops stay attached (e.g. busy), or a region's start can't be
        zeroed, the regions stay allocated and a warning says so; removing
        again later releases them.
        """
        for device in devices:
            await self.remove_device(device)
        if (pool := self.pool) is None:
            return
        await self._cmd(["sudo", "udevadm", "settle"], check=False)
        for attempt in range(_DETACH_CHECKS):
            if not (still_attached := self.owned_devices()):
                if kept := pool.release_owner(self.owner):
                    logger.warning(
                        f"{self.owner}: could not wipe pool regions "
                        f"{', '.join(kept)} on {pool.parent}; keeping them "
                        "allocated until a later remove succeeds"
                    )
                return
            if attempt < _DETACH_CHECKS - 1:
                await asyncio.sleep(_DETACH_RETRY_DELAY)
        logger.warning(
            f"{self.owner}: {', '.join(still_attached)} still attached to "
            f"{pool.parent}; keeping its pool regions allocated until a later "
            "remove succeeds"
        )

    async def create_device(self, device: str, index: int) -> None:
        await self._ensure_loop_module()
        await self.remove_device(device)
        try:
            await self._make_device(device, index)
        except Exception:
            # Removal finds devices from attached loops (pool) or backing
            # files (sparse), so a node left behind here would never be found.
            try:
                await self.remove_device(device)
            except Exception:
                logger.exception(f"{self.owner}: could not clean up {device}")
            raise

    async def _make_device(self, device: str, index: int) -> None:
        """Create the device node and attach it to its pool region or file."""
        device_pos = str(loop_number(device))
        await self._cmd(
            ["sudo", "mknod", "-m700", device, "b", "7", device_pos],
            check=True,
        )
        # Use host.user_ids() to get the container host's uid:gid (not the local process uid:gid)
        uid, gid = host.user_ids()
        await self._cmd(
            ["sudo", "chown", f"{uid}:{gid}", device],
            check=True,
        )
        if (pool := self.pool) is not None:
            offset = pool.get_or_allocate(self.owner, index, self.device_size)
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
            await self._cmd(["mkdir", "-p", str(self.image_dir)], check=True)
            await self._cmd(
                [
                    "sudo",
                    "dd",
                    "if=/dev/null",
                    f"of={image_path}",
                    "bs=1",
                    "count=0",
                    f"seek={self.device_size}",
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
        if host.device_mounted(device):
            await self._cmd(["umount", device], check=True)
        if host.path_exists(device):
            await self._cmd(["sudo", "losetup", "-d", device], check=False)
            await self._cmd(["sudo", "rm", "-f", device], check=False)
        if self.pool is None:
            await self._cmd(["rm", "-f", str(self._image_path(device))], check=False)

    async def _ensure_loop_module(self) -> None:
        proc = await self._cmd(["bash", "-c", "lsmod | grep -q loop"], check=False)
        if await proc.wait() != 0:
            await self._cmd(["sudo", "modprobe", "loop"])
