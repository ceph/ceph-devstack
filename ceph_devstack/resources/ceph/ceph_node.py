import shlex
import shutil
import sys
import uuid

from pathlib import Path

from ceph_devstack import DEFAULT_CEPH_IMAGE, logger
from ceph_devstack.host import host
from ceph_devstack.resources.ceph.build_config import CephBuildConfigMixin
from ceph_devstack.resources.ceph.host_loops import LoopDeviceMixin
from ceph_devstack.resources.container import Container

ENTRYPOINT_SCRIPT = Path(__file__).with_name("ceph-node-entrypoint.sh")
CLUSTER_ENTRYPOINT_NAME = "ceph-node-entrypoint.sh"
CLUSTER_DATA_NAMES = ("var", "fsid", "ceph.conf", CLUSTER_ENTRYPOINT_NAME)
CONTAINER_CLUSTER_DIR = "/var/lib/ceph-devstack/cluster"

BASE_CAPABILITIES = [
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


class CephNode(CephBuildConfigMixin, LoopDeviceMixin, Container):
    """Single-container Ceph cluster without cephadm.

    Builds the runtime image when ``repo`` is configured, creates host loop
    devices, and runs mon/mgr/osd inside one capability-scoped container via
    ``ceph-node-entrypoint.sh``.
    """

    _name = "ceph_node"
    trigger_udev = True

    def __init__(self, name: str = "", **kwargs):
        super().__init__(name, **kwargs)
        self._init_loop_devices()

    @property
    def config_key(self) -> str:
        return "ceph_node"

    @property
    def cluster_dir(self) -> Path:
        output_dir = self.config.get("output_dir", "")
        if output_dir:
            return Path(output_dir).expanduser().absolute()
        return self.data_dir

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
        return self.config_bool("dashboard", default=True)

    @property
    def dashboard_port(self) -> int:
        return int(self.config.get("dashboard_port", 8080))

    @property
    def dashboard_ssl(self) -> bool:
        return self.config_bool("dashboard_ssl")

    @property
    def dashboard_user(self) -> str:
        return self.config.get("dashboard_user", "admin")

    @property
    def dashboard_password(self) -> str:
        return self.config.get("dashboard_password", "admin")

    @property
    def dashboard_show_password(self) -> bool:
        return self.config_bool("dashboard_show_password")

    @property
    def create_cmd(self):
        host_cluster_dir = self.cluster_dir.resolve()
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
            ",".join(BASE_CAPABILITIES),
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
            "--health-cmd",
            f"CMD ceph --conf {self.container_cluster_dir}/ceph.conf -s",
            "--health-interval",
            "10s",
            "--health-retries",
            "30",
            "--health-timeout",
            "10s",
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

    async def _verify_local_image(self):
        """Verify that the configured local image exists."""
        if not self.image.startswith("localhost/"):
            logger.info(
                f"{self.name}: image_builder={self.image_builder} expects a "
                f"local image (got {self.image!r}); skipping"
            )
            return
        proc = await self.cmd(
            ["podman", "image", "exists", self.image],
            check=False,
        )
        if await proc.wait() != 0:
            raise FileNotFoundError(
                f"image_builder={self.image_builder} requires image "
                f"{self.image!r} built by ceph_builder"
            )

    def install_entrypoint(self):
        if not ENTRYPOINT_SCRIPT.is_file():
            raise FileNotFoundError(f"Entrypoint script not found: {ENTRYPOINT_SCRIPT}")
        shutil.copy2(ENTRYPOINT_SCRIPT, self.container_entrypoint)
        self.container_entrypoint.chmod(0o755)

    async def label_cluster_dir(self):
        if sys.platform == "darwin":
            return
        await self.cmd(
            ["chcon", "-Rt", "container_file_t", str(self.cluster_dir.resolve())],
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
        """Verify the runtime image exists (produced by ceph_builder)."""
        if not self.image.startswith("localhost/"):
            return
        await self._verify_local_image()

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
