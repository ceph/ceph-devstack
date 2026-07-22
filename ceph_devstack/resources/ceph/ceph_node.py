import asyncio
import os
import shlex
import shutil
import sys
import uuid

from pathlib import Path
from subprocess import CalledProcessError
from typing import List

from ceph_devstack import config, logger
from ceph_devstack.host import host
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner
from ceph_devstack.resources.ceph.host_loops import allocate_loop_devices
from ceph_devstack.resources.container import Container


DEFAULT_CEPH_IMAGE = "quay.io/ceph-ci/ceph:main"
ENTRYPOINT_SCRIPT = Path(__file__).with_name("ceph-node-entrypoint.sh")
CLUSTER_ENTRYPOINT_NAME = "ceph-node-entrypoint.sh"
CLUSTER_DATA_NAMES = ("var", "fsid", CLUSTER_ENTRYPOINT_NAME)
CONTAINER_CLUSTER_DIR = "/var/lib/ceph-devstack/cluster"

CEPH_NODE_CAPABILITIES = [
    "SYS_ADMIN",
    "NET_ADMIN",
    "SYS_TIME",
    "SYS_RAWIO",
    "MKNOD",
    "NET_RAW",
    "SETUID",
    "SETGID",
    "CHOWN",
    "SYS_PTRACE",
]


def expand_path(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path)))


class CephNode(Container):
    """Single-container Ceph cluster without cephadm.

    Builds the runtime image when ``repo`` is configured, creates host loop
    devices, and runs mon/mgr/osd inside one capability-scoped container via
    ``ceph-node-entrypoint.sh``.
    """

    _name = "ceph_node"
    stop_cmd = ["podman", "container", "stop", "{name}"]
    remove_cmd = ["podman", "container", "rm", "-f", "{name}"]
    start_cmd = ["podman", "container", "start", "{name}"]
    exists_cmd = ["podman", "container", "inspect", "{name}"]

    def __init__(self, name: str = ""):
        super().__init__(name)
        self.loop_device_count = self.config["loop_device_count"]
        self._devices: list[str] | None = None
        self._block_device_provisioner: BlockDeviceProvisioner | None = None

    @property
    def devices(self) -> list[str]:
        if self._devices is None:
            self._devices = allocate_loop_devices(
                self.name, self.loop_device_count, self.loop_img_dir
            )
        return self._devices

    @property
    def config_key(self) -> str:
        return "ceph_node"

    @property
    def cluster_dir(self) -> Path:
        return expand_path(self.config.get("output_dir", config["data_dir"]))

    @property
    def persistent_cache_dir(self) -> Path:
        # Lives beside cluster_dir so ``remove()`` can tear down cluster state
        # without wiping npm/sccache/dnf caches.
        return self.cluster_dir.parent / "cache"

    @property
    def loop_img_dir(self) -> Path:
        # Keep loop backing files outside cluster_dir so podman volume relabeling
        # does not touch them on container start (same layout as TestNode).
        return self.cluster_dir.parent / "disk_images"

    @property
    def legacy_loop_img_dir(self) -> Path:
        return self.cluster_dir / "disk_images"

    @property
    def container_entrypoint(self) -> Path:
        return self.cluster_dir / CLUSTER_ENTRYPOINT_NAME

    @property
    def container_entrypoint_script(self) -> str:
        return f"{self.container_cluster_dir}/{CLUSTER_ENTRYPOINT_NAME}"

    @property
    def container_cluster_dir(self) -> str:
        return CONTAINER_CLUSTER_DIR

    @property
    def repo(self) -> str:
        """CephNode doesn't have its own repo - it uses builder's artifacts."""
        return ""

    @property
    def image(self) -> str:
        """Container image to use for the Ceph node."""
        return self.config.get("image", DEFAULT_CEPH_IMAGE)

    @property
    def mon_id(self) -> str:
        return self.config.get("mon_id", "a")

    @property
    def mgr_id(self) -> str:
        return self.config.get("mgr_id", "x")

    @property
    def dashboard_enabled(self) -> bool:
        return self.config.get("dashboard", True) is not False

    @property
    def dashboard_port(self) -> int:
        return int(self.config.get("dashboard_port", 8080))

    @property
    def dashboard_ssl(self) -> bool:
        return bool(self.config.get("dashboard_ssl", False))

    @property
    def dashboard_user(self) -> str:
        return self.config.get("dashboard_user", "admin")

    @property
    def dashboard_password(self) -> str:
        return self.config.get("dashboard_password", "admin")

    @property
    def dashboard_show_password(self) -> bool:
        return self.config.get("dashboard_show_password", False) is True

    @property
    def image_builder(self) -> str:
        """Get image builder mode from config."""
        return self.config.get("image_builder", "binary-patch")

    @property
    def build_path(self) -> Path:
        """Get build path from config."""
        build_dir = self.config.get("build_dir", "build")
        repo = self.config.get("repo", "~/dev/ceph")
        return expand_path(repo) / build_dir

    @property
    def create_cmd(self):
        host_cluster_dir = str(self.cluster_dir.resolve())
        container_cluster_dir = self.container_cluster_dir
        entrypoint = self.container_entrypoint_script
        return [
            "podman",
            "container",
            "create",
            "-i",
            "--network",
            "host",
            "--cap-add",
            ",".join(CEPH_NODE_CAPABILITIES),
            "--security-opt",
            "unmask=/sys/dev/block",
            "-v",
            f"{host_cluster_dir}:{container_cluster_dir}",
            "-v",
            f"{host_cluster_dir}/var/lib/ceph:/var/lib/ceph",
            "-v",
            "/run/udev:/run/udev",
            "-v",
            "/sys/dev/block:/sys/dev/block",
            "-v",
            "/dev/fuse:/dev/fuse",
            "-v",
            "/dev/disk:/dev/disk",
            "--device",
            "/dev/net/tun",
            *[f"--device={device}" for device in self.devices],
            "-e",
            f"CLUSTER_DIR={container_cluster_dir}",
            "-e",
            f"MON_ID={self.mon_id}",
            "-e",
            f"MGR_ID={self.mgr_id}",
            "-e",
            f"OSD_DEVICES={','.join(self.devices)}",
            "-e",
            f"DASHBOARD_ENABLED={'true' if self.dashboard_enabled else 'false'}",
            "-e",
            f"DASHBOARD_PORT={self.dashboard_port}",
            "-e",
            f"DASHBOARD_SSL={'true' if self.dashboard_ssl else 'false'}",
            "-e",
            f"DASHBOARD_USER={self.dashboard_user}",
            "-e",
            f"DASHBOARD_PASSWORD={self.dashboard_password}",
            "-e",
            f"DASHBOARD_SHOW_PASSWORD={'true' if self.dashboard_show_password else 'false'}",
            "-e",
            f"CONTAINER_NAME={self.name}",
            "-e",
            "IBM_TELEMETRY_DISABLED=true",
            "--entrypoint",
            "/bin/bash",
            "--name",
            "{name}",
            "{image}",
            "-c",
            f". {shlex.quote(entrypoint)}",
        ]

    def _device_image(self, device: str) -> str:
        return f"{self.name}-{device.removeprefix('/dev/loop')}"

    def _binary_patch_cmd(self) -> List[str]:
        """Build command for binary-patch image creation."""
        # Base image is the target_image from config (what we're patching)
        base_image = self.config.get("target_image", DEFAULT_CEPH_IMAGE)
        return [
            "sudo",
            "../src/script/cpatch",
            "--base",
            base_image,
            "--target",
            self.image,
            "--core",
        ]

    async def _run_cmd(self, cmd: List[str], cwd: str):
        """Run a command and handle errors."""
        proc = await host.arun(
            cmd,
            cwd=Path(cwd).expanduser(),
            stream_output=True,
        )
        returncode = await proc.wait()
        if returncode != 0:
            stdout, stderr = await proc.log_failure(cmd)
            raise CalledProcessError(
                returncode,
                cmd,
                output=stdout or None,
                stderr=stderr or None,
            )

    async def _build_image_binary_patch(self):
        """Build runtime image using binary-patch method."""
        build_path = self.build_path
        logger.info(
            f"{self.name}: building {self.image} via binary-patch in {build_path}"
        )
        await self._run_cmd(self._binary_patch_cmd(), cwd=str(build_path))

    async def _build_image_package_build(self):
        """Build runtime image using package-build method."""
        raise NotImplementedError(
            "image_builder='package-build' requires ceph container/build.sh to consume "
            "locally-built packages; enable this once that support lands upstream"
        )

    async def _build_image(self):
        """Build the runtime image from builder artifacts."""
        builders = {
            "binary-patch": self._build_image_binary_patch,
            "package-build": self._build_image_package_build,
        }
        try:
            builder_method = builders[self.image_builder]
        except KeyError as exc:
            known = ", ".join(sorted(builders))
            raise ValueError(
                f"Unknown image_builder {self.image_builder!r}; known: {known}"
            ) from exc
        await builder_method()

    def install_entrypoint(self):
        if not ENTRYPOINT_SCRIPT.is_file():
            raise FileNotFoundError(f"Entrypoint script not found: {ENTRYPOINT_SCRIPT}")
        shutil.copy2(ENTRYPOINT_SCRIPT, self.container_entrypoint)
        self.container_entrypoint.chmod(0o755)

    async def label_cluster_dir(self):
        if sys.platform == "darwin":
            return
        await self.cmd(
            ["chcon", "-Rt", "container_file_t", str(self.cluster_dir)],
            check=False,
        )

    async def remove_legacy_loop_img_dir(self):
        legacy = self.legacy_loop_img_dir
        if not legacy.is_dir():
            return
        logger.info(
            f"{self.name}: removing legacy loop image dir at {legacy} "
            "(loop backing files now live outside the cluster mount)"
        )
        for device in self.devices:
            if not host.path_exists(device):
                continue
            proc = await self.cmd(["losetup", device], check=False)
            if proc and await proc.wait() == 0:
                await self.cmd(["sudo", "losetup", "-d", device], check=False)
        shutil.rmtree(legacy)

    async def remove_cluster_data(self):
        cluster_dir = self.cluster_dir.resolve()
        if not cluster_dir.exists():
            return
        paths = [
            cluster_dir / name
            for name in CLUSTER_DATA_NAMES
            if (cluster_dir / name).exists()
        ]
        if not paths:
            return
        logger.info(f"{self.name}: removing cluster data at {cluster_dir}")
        for path in paths:
            await self.cmd(
                ["podman", "unshare", "rm", "-rf", str(path)],
                check=False,
            )

    async def build(self):
        """Build runtime image from local build artifacts."""
        # Only build if we have a local image tag (localhost/...)
        if not self.image.startswith("localhost/"):
            return

        # Check if we have a local repo configured for building
        repo = self.config.get("repo")
        if not repo:
            logger.info(f"{self.name}: skipping build (no repo configured)")
            return

        repo_path = expand_path(repo)
        if not repo_path.exists():
            logger.error(f"{self.name}: repo not found at {repo_path}")
            return

        build_path = self.build_path
        if not build_path.exists():
            logger.error(f"{self.name}: build directory not found at {build_path}")
            return

        logger.info(f"{self.name}: Building runtime image from local build artifacts")
        await self._build_image()

    async def create(self):
        logger.info(f"{self.name}: preparing cluster at {self.cluster_dir}")
        self.cluster_dir.mkdir(parents=True, exist_ok=True)
        (self.cluster_dir / "var/lib/ceph").mkdir(parents=True, exist_ok=True)
        (self.cluster_dir / "var/lib/ceph/mon" / f"ceph-{self.mon_id}").mkdir(
            parents=True, exist_ok=True
        )
        (self.cluster_dir / "var/lib/ceph/mgr" / f"ceph-{self.mgr_id}").mkdir(
            parents=True, exist_ok=True
        )
        if not (self.cluster_dir / "fsid").exists():
            (self.cluster_dir / "fsid").write_text(f"{uuid.uuid4()}\n")
        self.install_entrypoint()
        await self.remove_legacy_loop_img_dir()
        await self.label_cluster_dir()
        logger.info(
            f"{self.name}: creating {self.loop_device_count} loop devices "
            f"({self.config['loop_device_size']} each)"
        )
        await self.create_loop_devices()
        await super().create()

    async def remove(self):
        await super().remove()
        await self.remove_loop_devices()
        await self.remove_legacy_loop_img_dir()
        await self.remove_cluster_data()

    async def is_running(self):
        if not await self.exists():
            return False
        proc = await self.cmd(
            [
                "podman",
                "exec",
                self.name,
                "ceph",
                "--conf",
                f"{self.container_cluster_dir}/ceph.conf",
                "-s",
            ],
            check=False,
        )
        return proc is not None and await proc.wait() == 0

    async def wait(self):
        if not await self.exists():
            return 1
        for _ in range(60):
            if await self.is_running():
                return 0
            await asyncio.sleep(5)
        return 1

    def _block_provisioner(self) -> BlockDeviceProvisioner:
        if self._block_device_provisioner is None:
            self._block_device_provisioner = BlockDeviceProvisioner(
                self.name,
                image_dir=self.loop_img_dir,
                file_size=self.config["loop_device_size"],
                cmd=self.cmd,
                trigger_udev=True,
            )
        return self._block_device_provisioner

    async def create_loop_devices(self):
        if self.devices:
            numbers = [int(device.removeprefix("/dev/loop")) for device in self.devices]
            logger.info(
                f"{self.name}: host loop devices "
                f"{numbers[0]}-{numbers[-1]} "
                f"({self.config['loop_device_size']} each)"
            )
        await self._block_provisioner().create_devices(self.devices)

    async def remove_loop_devices(self):
        await self._block_provisioner().remove_devices(self.devices)
        self._devices = None
