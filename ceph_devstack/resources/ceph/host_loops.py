"""Host loop device discovery, allocation, and provisioning mixin."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ceph_devstack import logger
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner


class LoopDeviceMixin:
    """Mixin for Container subclasses that manage host loop devices.

    Subclasses must define a ``loop_img_dir`` property returning a ``Path``.
    Set ``trigger_udev = True`` on the class to trigger udev after device creation.
    """

    name: str
    config: dict[str, Any]
    cmd: Any
    trigger_udev: bool = False
    _allocated: ClassVar[set[int]] = set()

    @property
    def loop_img_dir(self) -> Path:
        raise NotImplementedError

    @staticmethod
    def host_loop_path(number: int) -> str:
        return f"/dev/loop{number}"

    @staticmethod
    def _loop_number_from_backing_name(name: str) -> int | None:
        if "-" not in name:
            return None
        loop_id = name.rsplit("-", 1)[-1]
        if not loop_id.isdigit():
            return None
        return int(loop_id)

    @staticmethod
    def _sysfs_loop_in_use(block_name: str) -> bool:
        block = Path("/sys/class/block") / block_name
        backing = block / "loop" / "backing_file"
        if backing.is_file():
            text = backing.read_text().strip()
            if text not in ("", "(deleted)"):
                return True
        size_file = block / "size"
        if not size_file.is_file():
            return False
        try:
            return int(size_file.read_text()) > 0
        except ValueError:
            return False

    @classmethod
    def _discover_used_from_sysfs(cls) -> set[int]:
        used: set[int] = set()
        block_dir = Path("/sys/class/block")
        if not block_dir.is_dir():
            return used
        for entry in block_dir.iterdir():
            name = entry.name
            if not name.startswith("loop"):
                continue
            suffix = name.removeprefix("loop")
            if suffix.isdigit() and cls._sysfs_loop_in_use(name):
                used.add(int(suffix))
        return used

    @classmethod
    def _discover_used_from_backing_files(cls, image_dir: Path) -> set[int]:
        used: set[int] = set()
        if not image_dir.is_dir():
            return used
        for path in image_dir.iterdir():
            if not path.is_file():
                continue
            if (number := cls._loop_number_from_backing_name(path.name)) is not None:
                used.add(number)
        return used

    @classmethod
    def discover_used_loop_numbers(cls, image_dir: Path) -> set[int]:
        """Loop numbers already attached on the host or claimed by backing files."""
        return cls._discover_used_from_sysfs() | cls._discover_used_from_backing_files(
            image_dir
        )

    @classmethod
    def owner_loop_numbers(cls, owner: str, image_dir: Path) -> list[int]:
        """Existing backing-file loop numbers for an owner, lowest first."""
        if not image_dir.is_dir():
            return []
        numbers: list[int] = []
        prefix = f"{owner}-"
        for path in image_dir.iterdir():
            if not path.is_file() or not path.name.startswith(prefix):
                continue
            if (number := cls._loop_number_from_backing_name(path.name)) is not None:
                numbers.append(number)
        return sorted(numbers)

    @classmethod
    def reset_allocation(cls) -> None:
        """Clear the cross-call allocation tracker (for testing or between runs)."""
        cls._allocated = set()

    @classmethod
    def allocate_loop_numbers(
        cls, owner: str, count: int, image_dir: Path
    ) -> list[int]:
        """Pick loop numbers for an owner from config count and host availability."""
        used = cls.discover_used_loop_numbers(image_dir) | cls._allocated
        reclaimed = cls.owner_loop_numbers(owner, image_dir)
        numbers: list[int] = []
        for index in range(count):
            if index < len(reclaimed):
                numbers.append(reclaimed[index])
                continue
            candidate = 0
            while candidate in used or candidate in numbers:
                candidate += 1
            numbers.append(candidate)
            used.add(candidate)
        cls._allocated.update(numbers)
        return numbers

    @classmethod
    def allocate_loop_devices(
        cls, owner: str, count: int, image_dir: Path
    ) -> list[str]:
        return [
            cls.host_loop_path(number)
            for number in cls.allocate_loop_numbers(owner, count, image_dir)
        ]

    def _init_loop_devices(self):
        self.loop_device_count: int = self.config["loop_device_count"]
        self._devices: list[str] | None = None
        self._block_device_provisioner: BlockDeviceProvisioner | None = None

    @property
    def devices(self) -> list[str]:
        if self._devices is None:
            self._devices = self.allocate_loop_devices(
                self.name, self.loop_device_count, self.loop_img_dir
            )
        return self._devices

    def _block_provisioner(self) -> BlockDeviceProvisioner:
        if self._block_device_provisioner is None:
            self._block_device_provisioner = BlockDeviceProvisioner(
                self.name,
                image_dir=self.loop_img_dir,
                file_size=self.config["loop_device_size"],
                cmd=self.cmd,
                trigger_udev=self.trigger_udev,
            )
        return self._block_device_provisioner

    async def create_loop_devices(self):
        if self.devices:
            numbers = [int(d.removeprefix("/dev/loop")) for d in self.devices]
            logger.info(
                f"{self.name}: host loop devices "
                f"{numbers[0]}-{numbers[-1]} "
                f"({self.config['loop_device_size']} each)"
            )
        await self._block_provisioner().create_devices(self.devices)

    async def remove_loop_devices(self):
        await self._block_provisioner().remove_devices(self.devices)
        self._devices = None
