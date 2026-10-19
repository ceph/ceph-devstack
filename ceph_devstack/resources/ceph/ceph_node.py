import json
import shutil

from pathlib import Path

from ceph_devstack import logger
from ceph_devstack.block_pool import shared_pool
from ceph_devstack.resources.ceph.containers import SELINUX_RELABEL_SUFFIX
from ceph_devstack.resources.ceph.host_loops import LoopDeviceMixin
from ceph_devstack.resources.container import Container

ENTRYPOINT_SCRIPT = Path(__file__).with_name("ceph-node-entrypoint.sh")
DASHBOARD_PASSWORD_FILE = "dashboard-password"
CONTAINER_CLUSTER_DIR = "/var/lib/ceph-devstack/cluster"
CONTAINER_CEPH_CONF = f"{CONTAINER_CLUSTER_DIR}/ceph.conf"

# CephNode starts from ``--cap-drop=all``; every capability here must be
# justified by something the entrypoint or the Ceph daemons actually do.
# Notably absent: SYS_ADMIN. Nothing here mounts, unshares, or needs
# host-namespace block ioctls (which rootless SYS_ADMIN could not grant anyway).
CAPABILITIES = [
    # chown of OSD dirs, keyrings, and loop devices to ceph:ceph
    "CHOWN",
    # root reading ceph-owned files/devices after the chown; without it the
    # BlueStore label probe cannot open the (0700) loop devices and no OSD starts
    "DAC_OVERRIDE",
    # ceph-mon/mgr/osd --setuser ceph --setgroup ceph
    "SETUID",
    "SETGID",
]


class CephNode(LoopDeviceMixin, Container):
    """Single-container Ceph cluster without cephadm.

    Creates host loop devices and runs mon/mgr/osd inside one
    capability-scoped container via ``ceph-node-entrypoint.sh``, which owns
    all cluster bootstrap state under ``cluster_dir``.
    """

    _name = "ceph_node"
    trigger_udev = True

    def __init__(self, name: str = "", **kwargs):
        super().__init__(name, **kwargs)
        self._init_loop_devices()

    @property
    def cluster_dir(self) -> Path:
        """Config, keyrings and daemon data; deleted by remove()."""
        return self.data_dir / "cluster"

    @property
    def loop_img_dir(self) -> Path:
        return self.data_dir / "disk_images"

    def _flag(self, key: str) -> str:
        return "true" if self.config_bool(key) else "false"

    @property
    def entrypoint_env(self) -> dict[str, str]:
        """The entrypoint's settings; it requires every one of them."""
        return {
            "CLUSTER_DIR": CONTAINER_CLUSTER_DIR,
            "CEPH_CONF": CONTAINER_CEPH_CONF,
            "OSD_DEVICES": ",".join(self.devices),
            "DASHBOARD_ENABLED": self._flag("dashboard"),
            "DASHBOARD_PORT": str(self.config["dashboard_port"]),
            "DASHBOARD_SSL": self._flag("dashboard_ssl"),
            "DASHBOARD_USER": self.config["dashboard_user"],
            "DASHBOARD_PASSWORD_FILE": f"{CONTAINER_CLUSTER_DIR}/{DASHBOARD_PASSWORD_FILE}",
            "DASHBOARD_SHOW_PASSWORD": self._flag("dashboard_show_password"),
            "CONTAINER_NAME": self.name,
            "IBM_TELEMETRY_DISABLED": "true",
        }

    @property
    def create_cmd(self):
        cluster_dir = self.cluster_dir
        entrypoint = f"{CONTAINER_CLUSTER_DIR}/{ENTRYPOINT_SCRIPT.name}"
        return [
            "podman",
            "container",
            "create",
            "-i",
            "--network",
            "host",
            "--cap-drop",
            "all",
            "--cap-add",
            ",".join(CAPABILITIES),
            "--security-opt",
            "no-new-privileges",
            "--security-opt",
            "unmask=/sys/dev/block",
            "-v",
            f"{cluster_dir}:{CONTAINER_CLUSTER_DIR}{SELINUX_RELABEL_SUFFIX}",
            "-v",
            f"{cluster_dir}/var/lib/ceph:/var/lib/ceph{SELINUX_RELABEL_SUFFIX}",
            # Read-only udev database: ceph-osd looks up device properties
            # (vendor, model, serial) through libudev.
            "-v",
            "/run/udev:/run/udev:ro",
            "-v",
            "/sys/dev/block:/sys/dev/block",
            *[f"--device={device}" for device in self.devices],
            *[
                arg
                for key, value in self.entrypoint_env.items()
                for arg in ("-e", f"{key}={value}")
            ],
            "--health-cmd",
            "CMD ceph -s",
            "--health-interval",
            "10s",
            "--health-retries",
            "30",
            "--health-timeout",
            "10s",
            "--entrypoint",
            json.dumps(["/bin/bash", entrypoint]),
            "--name",
            "{name}",
            "{image}",
        ]

    async def build(self):
        """Nothing to build here; check that a localhost/ image exists."""
        if not self.image.startswith("localhost/"):
            return
        proc = await self.cmd(["podman", "image", "exists", self.image])
        if await proc.wait() != 0:
            raise FileNotFoundError(f"Local image {self.image!r} not found")

    def prepare_cluster_dir(self):
        """Install what the entrypoint needs; it bootstraps the cluster itself."""
        logger.info(f"{self.name}: preparing cluster at {self.cluster_dir}")
        # Source of the /var/lib/ceph bind mount, so it must exist up front.
        (self.cluster_dir / "var/lib/ceph").mkdir(parents=True, exist_ok=True)
        shutil.copy2(ENTRYPOINT_SCRIPT, self.cluster_dir / ENTRYPOINT_SCRIPT.name)
        # A file rather than an env var, which podman inspect would show.
        password_file = self.cluster_dir / DASHBOARD_PASSWORD_FILE
        password_file.touch(mode=0o600)
        password_file.chmod(0o600)
        password_file.write_text(self.config["dashboard_password"])

    async def remove_cluster_dir(self):
        # The daemons chowned files to ceph inside the user namespace.
        proc = await self.cmd(["podman", "unshare", "rm", "-rf", str(self.cluster_dir)])
        if proc.returncode != 0:
            logger.warning(
                f"{self.name}: failed to remove {self.cluster_dir}; "
                "please remove it manually"
            )

    async def create(self):
        if not await self.exists():
            if shared_pool() is None:
                # Sparse backing files are recreated blank, so cluster state
                # left by an earlier container would no longer match them.
                await self.remove_cluster_dir()
            self.prepare_cluster_dir()
            await self.create_loop_devices()
        await super().create()

    async def remove(self):
        await super().remove()
        await self.remove_loop_devices()
        await self.remove_cluster_dir()
