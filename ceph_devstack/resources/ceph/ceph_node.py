import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import uuid

from pathlib import Path
from subprocess import CalledProcessError
from typing import List

import tomlkit

from ceph_devstack import PROJECT_ROOT, config, logger
from ceph_devstack.host import host
from ceph_devstack.resources.ceph.block_devices import BlockDeviceProvisioner
from ceph_devstack.resources.ceph.host_loops import allocate_loop_devices
from ceph_devstack.resources.container import Container


DEFAULT_CEPH_IMAGE = "quay.io/ceph-ci/ceph:main"
DEFAULT_BASE_IMAGE = "quay.io/ceph-ci/ceph:main"
PACKAGE_SCCACHE_CONF = PROJECT_ROOT / "sccache.conf"
PACKAGE_SCCACHE_S3_CONF = PROJECT_ROOT / "sccache-s3.conf"
CONTAINER_SCCACHE_DIR = "/sccache"
CONTAINER_GIT_METADATA_DIR = "/git-metadata"
BWC_HOMEDIR = "/ceph"
REPO_DEVSTACK_DIR = ".ceph-devstack"
BUILD_ENV_NAME = "build.env"
ENTRYPOINT_SCRIPT = Path(__file__).with_name("ceph-node-entrypoint.sh")
CLUSTER_ENTRYPOINT_NAME = "ceph-node-entrypoint.sh"
CLUSTER_DATA_NAMES = ("var", "fsid", CLUSTER_ENTRYPOINT_NAME)
CONTAINER_CLUSTER_DIR = "/var/lib/ceph-devstack/cluster"

DEFAULT_COMPILE_STEPS = {
    "cpatch": ["build"],
    "container": ["packages"],
}

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


def git_worktree_info(repo: Path) -> tuple[Path, str] | None:
    """Return the main .git directory and worktree name for a linked worktree."""
    git_path = repo.resolve() / ".git"
    if not git_path.is_file():
        return None
    text = git_path.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return None
    admin_dir = Path(text.split(":", 1)[1].strip())
    if admin_dir.parent.name != "worktrees":
        raise ValueError(f"unexpected git worktree gitdir: {admin_dir}")
    main_git_dir = admin_dir.parent.parent
    if not main_git_dir.is_dir():
        raise FileNotFoundError(f"git metadata dir not found: {main_git_dir}")
    return main_git_dir.resolve(), admin_dir.name


def worktree_submodule_git_mounts(
    repo: Path,
    worktree_name: str,
    git_overlay_dir: Path,
) -> list[str]:
    """Return podman mounts that rewrite submodule ``.git`` files for ``/ceph``."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "submodule", "foreach", "--quiet", "echo $sm_path"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []

    overlay_dir = git_overlay_dir / "submodules"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    mounts: list[str] = []
    for sm_path in proc.stdout.splitlines():
        sm_path = sm_path.strip()
        if not sm_path:
            continue
        gitdir = (
            f"{CONTAINER_GIT_METADATA_DIR}/worktrees/{worktree_name}/modules/{sm_path}"
        )
        overlay = overlay_dir / f"{sm_path.replace('/', '__')}.git"
        overlay.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        mounts.append(f"--volume={overlay}:{BWC_HOMEDIR}/{sm_path}/.git:Z,ro")
    return mounts


def worktree_container_mounts(
    repo: Path,
    main_git_dir: Path,
    worktree_name: str,
) -> list[str]:
    """Return podman mounts that make a linked worktree usable at ``/ceph``."""
    repo = repo.resolve()
    main_git_dir = main_git_dir.resolve()
    git_overlay_dir = repo / REPO_DEVSTACK_DIR / "git"
    git_overlay_dir.mkdir(parents=True, exist_ok=True)

    dot_git = git_overlay_dir / "dot-git"
    dot_git.write_text(
        f"gitdir: {CONTAINER_GIT_METADATA_DIR}/worktrees/{worktree_name}\n",
        encoding="utf-8",
    )

    admin_gitdir = git_overlay_dir / "gitdir"
    admin_gitdir.write_text(f"{BWC_HOMEDIR}/.git\n", encoding="utf-8")

    mounts = [
        f"--volume={main_git_dir}:{CONTAINER_GIT_METADATA_DIR}:Z,ro",
        f"--volume={dot_git}:{BWC_HOMEDIR}/.git:Z,ro",
        f"--volume={admin_gitdir}:{CONTAINER_GIT_METADATA_DIR}/worktrees/{worktree_name}/gitdir:Z,ro",
    ]
    mounts.extend(worktree_submodule_git_mounts(repo, worktree_name, git_overlay_dir))
    return mounts


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
    def base_image(self) -> str:
        return self.config.get("base_image", DEFAULT_BASE_IMAGE)

    @property
    def build_subdir(self) -> str:
        build_dir = self.config.get("build_dir", "build")
        if not build_dir:
            return "build"
        path = Path(os.path.expanduser(str(build_dir)))
        if path.is_absolute() and self.repo:
            repo = Path(self.repo).resolve()
            try:
                return str(path.resolve().relative_to(repo))
            except ValueError:
                return path.name
        return str(build_dir).strip("/")

    @property
    def build_path(self) -> Path:
        if not self.repo:
            return Path()
        return Path(self.repo) / self.build_subdir

    @property
    def image_builder(self) -> str:
        return self.config.get("image_builder", "cpatch")

    @property
    def compile_steps(self) -> List[str]:
        default = DEFAULT_COMPILE_STEPS.get(self.image_builder, ["build"])
        return list(self.config.get("build_steps", default))

    @property
    def sccache_enabled(self) -> bool:
        return self.config.get("sccache", True) is not False

    @property
    def sccache_mode(self) -> str:
        return str(self.config.get("sccache_mode", "local")).lower()

    @property
    def sccache_rw_mode(self) -> bool:
        """Whether to use sccache in read-write mode (requires credentials)."""
        return self.config.get("sccache_rw_mode", False) is True

    @property
    def sccache_debug(self) -> bool:
        return self.config.get("sccache_debug", False) is True

    @property
    def sccache_cache_path(self) -> Path:
        if custom := self.config.get("sccache_cache_path"):
            return expand_path(custom)
        return self.persistent_cache_dir / "sccache"

    @property
    def npm_cache_enabled(self) -> bool:
        return self.config.get("npm_cache", True) is not False

    @property
    def npm_cache_path(self) -> Path | None:
        if not self.npm_cache_enabled:
            return None
        if custom := self.config.get("npm_cache_path"):
            return expand_path(custom)
        return self.persistent_cache_dir / "npm"

    @property
    def dnf_cache_path(self) -> Path | None:
        if self.config.get("dnf_cache", False) is not True:
            return None
        if custom := self.config.get("dnf_cache_path"):
            return expand_path(custom)
        return self.persistent_cache_dir / "dnf"

    def _build_cache_args(self) -> list[str]:
        """Return build-with-container.py cache flags for persistent build caches."""
        args: list[str] = []

        npm_cache = self.npm_cache_path
        if npm_cache is not None:
            npm_cache.mkdir(parents=True, exist_ok=True)
            args.extend(["--npm-cache-path", str(npm_cache)])

        dnf_cache = self.dnf_cache_path
        if dnf_cache is not None:
            dnf_cache.mkdir(parents=True, exist_ok=True)
            args.extend(["--dnf-cache-path", str(dnf_cache)])

        return args

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
            "-v",
            "/dev/null:/sys/class/dmi/id/board_serial",
            "-v",
            "/dev/null:/sys/class/dmi/id/chassis_serial",
            "-v",
            "/dev/null:/sys/class/dmi/id/product_serial",
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

    def _get_s3_credentials_env(self) -> list[str]:
        """Get S3 credential environment variables for read-write mode."""
        aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        if not aws_access_key or not aws_secret_key:
            raise ValueError(
                "sccache_rw_mode=true requires AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY to be set in the environment"
            )
        return [
            f"AWS_ACCESS_KEY_ID={aws_access_key}",
            f"AWS_SECRET_ACCESS_KEY={aws_secret_key}",
            "SCCACHE_S3_NO_CREDENTIALS=false",
            "SCCACHE_S3_RW_MODE=READ_WRITE",
        ]

    def _sccache_build_env(self, repo: Path) -> tuple[list[str], list[str]]:
        if not self.sccache_enabled:
            return [], []

        homedir = BWC_HOMEDIR
        if custom_conf := self.config.get("sccache_conf"):
            conf_src = expand_path(custom_conf)
            use_local_cache = self.sccache_mode == "local"
        elif self.sccache_mode == "s3":
            conf_src = PACKAGE_SCCACHE_S3_CONF
            use_local_cache = False
        else:
            conf_src = PACKAGE_SCCACHE_CONF
            use_local_cache = True
        if not conf_src.is_file():
            raise FileNotFoundError(f"sccache config not found: {conf_src}")

        # Parse and modify config for S3 mode to set no_credentials correctly
        conf_content = conf_src.read_text()
        if self.sccache_mode == "s3":
            conf_data = tomlkit.parse(conf_content)
            # Set no_credentials based on whether we're using read-write mode
            conf_data["cache"]["s3"]["no_credentials"] = not self.sccache_rw_mode
            conf_content = tomlkit.dumps(conf_data)
        (repo / "sccache.conf").write_text(conf_content)

        lines = [
            "SCCACHE=true",
            f"SCCACHE_CONF={homedir}/sccache.conf",
            f"SCCACHE_ERROR_LOG={homedir}/.ceph-devstack/sccache_log.txt",
            "CEPH_BUILD_NORMALIZE_PATHS=true",
        ]
        if self.sccache_debug:
            lines.append("SCCACHE_LOG=debug")

        extra_args: list[str] = []
        if use_local_cache:
            cache_path = self.sccache_cache_path
            cache_path.mkdir(parents=True, exist_ok=True)
            extra_args.append(f"--volume={cache_path}:{CONTAINER_SCCACHE_DIR}:Z")
            lines.append(f"SCCACHE_DIR={CONTAINER_SCCACHE_DIR}")
            cache_size = self.config.get("sccache_cache_size", "100G")
            lines.append(f"SCCACHE_CACHE_SIZE={cache_size}")
        elif self.sccache_mode == "s3":
            if self.sccache_rw_mode:
                lines.extend(self._get_s3_credentials_env())
            else:
                # Read-only mode (default)
                lines.extend(
                    [
                        "SCCACHE_S3_NO_CREDENTIALS=true",
                        "SCCACHE_S3_RW_MODE=READ_ONLY",
                    ]
                )
        return lines, extra_args

    def _prepare_build_env(self) -> tuple[Path | None, list[str]]:
        if not self.repo:
            return None, []
        repo = Path(self.repo)
        extra_args: list[str] = []

        worktree = git_worktree_info(repo)
        if worktree is not None:
            main_git_dir, worktree_name = worktree
            extra_args.extend(
                worktree_container_mounts(repo, main_git_dir, worktree_name)
            )

        lines, sccache_extra = self._sccache_build_env(repo)
        extra_args.extend(sccache_extra)

        if not lines:
            return None, extra_args

        devstack_dir = repo / REPO_DEVSTACK_DIR
        devstack_dir.mkdir(exist_ok=True)
        env_path = devstack_dir / BUILD_ENV_NAME
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return env_path, extra_args

    def _compile_cmd(
        self,
        env_file: Path | None = None,
        extra_args: List[str] | None = None,
    ) -> List[str]:
        distro = self.config.get("build_distro", "centos9")
        script = str(Path(self.repo) / "src/script/build-with-container.py")
        python_cmd = "python3" if host.type == "remote" else sys.executable
        cmd = [
            python_cmd,
            script,
            "-d",
            distro,
            "-b",
            self.build_subdir,
            "--homedir",
            BWC_HOMEDIR,
        ]
        for step in self.compile_steps:
            cmd.extend(["-e", step])
        if env_file is not None:
            cmd.extend(["--env-file", str(env_file)])
        cmd.extend(self._build_cache_args())
        for extra in extra_args or []:
            cmd.append(f"--extra={extra}")
        return cmd

    def _cpatch_cmd(self) -> List[str]:
        return [
            "sudo",
            "../src/script/cpatch",
            "--base",
            self.base_image,
            "--target",
            self.image,
            "--core",
        ]

    def _git_value(self, args: str) -> str:
        return subprocess.check_output(
            f"git {args}".split(),
            cwd=self.repo,
            text=True,
        ).strip()

    async def _run_cmd(self, cmd: List[str], cwd: str):
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

    async def _compile(self):
        logger.info(
            f"{self.name}: compiling ceph via build-with-container.py in {self.repo}"
        )
        env_file, extra_args = self._prepare_build_env()
        await self._run_cmd(
            self._compile_cmd(env_file=env_file, extra_args=extra_args),
            cwd=self.repo,
        )

    def _verify_build_tree(self):
        build_path = self.build_path
        if (build_path / "build.ninja").exists() or (build_path / "Makefile").exists():
            return
        raise FileNotFoundError(
            f"Ceph build dir {build_path} missing Makefile or build.ninja "
            "after build-with-container.py"
        )

    async def _build_image_cpatch(self):
        self._verify_build_tree()
        build_path = self.build_path
        logger.info(f"{self.name}: building {self.image} via cpatch in {build_path}")
        await self._run_cmd(self._cpatch_cmd(), cwd=str(build_path))

    async def _build_image_container(self):
        raise NotImplementedError(
            "image_builder='container' requires ceph container/build.sh to consume "
            "locally-built packages; enable this once that support lands upstream"
        )

    async def _build_image(self):
        builders = {
            "cpatch": self._build_image_cpatch,
            "container": self._build_image_container,
        }
        try:
            builder = builders[self.image_builder]
        except KeyError as exc:
            known = ", ".join(sorted(builders))
            raise ValueError(
                f"Unknown image_builder {self.image_builder!r}; known: {known}"
            ) from exc
        await builder()

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
        if not self.should_build:
            return
        await self._compile()
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
