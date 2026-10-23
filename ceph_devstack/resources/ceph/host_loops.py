"""Host loop device discovery, allocation, and provisioning mixin."""

from pathlib import Path
from typing import Any, ClassVar

from ceph_devstack import base_data_dir, logger
from ceph_devstack.block_pool import shared_pool
from ceph_devstack.resources.ceph.block_devices import (
    BlockDeviceProvisioner,
    attached_loops,
    backing_files,
    loop_number,
    loop_path,
    stack_image_dirs,
)


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

    @classmethod
    def discover_used_loop_numbers(cls, image_dir: Path) -> set[int]:
        """Loop numbers attached on the host or claimed by backing files.

        Backing files of every stack count, not just image_dir's: after a
        reboot the loops are detached, but another stack's files still claim
        their numbers.
        """
        attached = {number for number, _, _ in attached_loops()}
        image_dirs = {image_dir, *stack_image_dirs(base_data_dir())}
        return attached | {n for d in image_dirs for _, n in backing_files(d)}

    @classmethod
    def owner_loop_numbers(cls, owner: str, image_dir: Path) -> list[int]:
        """Existing backing-file loop numbers for an owner, lowest first."""
        return sorted(n for o, n in backing_files(image_dir) if o == owner)

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
            loop_path(number)
            for number in cls.allocate_loop_numbers(owner, count, image_dir)
        ]

    def _init_loop_devices(self) -> None:
        self.loop_device_count: int = self.config["loop_device_count"]
        self._devices: list[str] | None = None
        self._block_device_provisioner: BlockDeviceProvisioner | None = None

    def allocate_devices(self) -> list[str]:
        """Pick this container's host loop devices now, if not chosen already."""
        if self._devices is None:
            self._devices = self.allocate_loop_devices(
                self.name, self.loop_device_count, self.loop_img_dir
            )
        return self._devices

    @property
    def devices(self) -> list[str]:
        return self.allocate_devices()

    def _block_provisioner(self) -> BlockDeviceProvisioner:
        if self._block_device_provisioner is None:
            self._block_device_provisioner = BlockDeviceProvisioner(
                self.name,
                image_dir=self.loop_img_dir,
                file_size=self.config["loop_device_size"],
                cmd=self.cmd,
                trigger_udev=self.trigger_udev,
                pool=shared_pool(),
            )
        return self._block_device_provisioner

    async def create_loop_devices(self):
        if self.devices:
            numbers = ", ".join(str(loop_number(d)) for d in self.devices)
            logger.info(
                f"{self.name}: host loop devices {numbers} "
                f"({self.config['loop_device_size']} each)"
            )
        await self._block_provisioner().create_devices(self.devices)

    async def remove_loop_devices(self):
        """Detach the devices this container holds on the host.

        Devices are found from host state (plus any picked earlier in this
        process), so removal never allocates fresh numbers that might belong
        to someone else. Their numbers are then released for reallocation.
        """
        provisioner = self._block_provisioner()
        devices = set(provisioner.owned_devices()) | set(self._devices or [])
        await provisioner.remove_devices(sorted(devices, key=loop_number))
        self._allocated.difference_update(loop_number(d) for d in devices)
        self._devices = None
