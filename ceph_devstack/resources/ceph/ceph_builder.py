"""CephBuilder resource for managing Ceph compilation and build artifacts."""

import os
import sys
from pathlib import Path
from subprocess import CalledProcessError
from typing import List, Type

import tomlkit

from ceph_devstack import DEFAULT_CEPH_IMAGE, PROJECT_ROOT, logger
from ceph_devstack.distros import get_package_type
from ceph_devstack.host import host
from ceph_devstack.resources.ceph.container_repo import (
    build_sh_env,
    find_rpm_packages_dir,
    runtime_image_tag,
    serve_yum_repo,
)
from ceph_devstack.resources import PodmanResource, StackResource
from ceph_devstack.resources.ceph.build_config import CephBuildConfigMixin
from ceph_devstack.resources.utils import host_arch, normalize_distro

PACKAGE_SCCACHE_CONF = PROJECT_ROOT / "sccache.conf"
PACKAGE_SCCACHE_S3_CONF = PROJECT_ROOT / "sccache-s3.conf"
CONTAINER_SCCACHE_DIR = "/sccache"
CONTAINER_GIT_METADATA_DIR = "/git-metadata"
BWC_HOMEDIR = "/ceph"
REPO_DEVSTACK_DIR = ".ceph-devstack"
BUILD_ENV_NAME = "build.env"
GCC_TOOLSET_ENV_NAME = "gcc-toolset-env.sh"
GCC_TOOLSET_ENV_SCRIPT = (
    "for f in $(ls -1d /opt/rh/gcc-toolset-*/enable 2>/dev/null"
    ' | sort -t- -k4 -n -r); do source "$f"; break; done\n'
)

DEFAULT_COMPILE_STEPS = {
    "binary-patch": ["build"],
    "package-build": ["packages"],
}

DISTRO_PYTHON_VERSIONS: dict[str, str] = {
    "centos9": "3.9",
    "centos10": "3.12",
    "rocky10": "3.12",
}

PYTHON_VERSION_DISTROS: dict[str, str] = {
    "3.9": "centos9",
    "3.12": "centos10",
}


class CephBuilder(CephBuildConfigMixin, PodmanResource):
    """Manages Ceph compilation and build artifacts.

    This resource handles:
    - Builder container image management (via Containerfile.ceph)
    - Compilation via build-with-container.py
    - Build cache management (sccache, npm, dnf)
    - Build artifact production
    """

    _name = "ceph_builder"

    def __init__(self, name: str = "", **kwargs):
        super().__init__(name, **kwargs)
        self._persistent_cache_dir: Path | None = None
        self._resolved_distro: str | None = None

    @staticmethod
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

    @staticmethod
    def _worktree_submodule_git_mounts(
        repo: Path,
        worktree_name: str,
        git_overlay_dir: Path,
    ) -> list[str]:
        """Return podman mounts that rewrite submodule ``.git`` files for ``/ceph``."""
        import subprocess

        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "submodule",
                "foreach",
                "--quiet",
                "echo $sm_path",
            ],
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
            gitdir = f"{CONTAINER_GIT_METADATA_DIR}/worktrees/{worktree_name}/modules/{sm_path}"
            overlay = overlay_dir / f"{sm_path.replace('/', '__')}.git"
            overlay.write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
            mounts.append(f"--volume={overlay}:{BWC_HOMEDIR}/{sm_path}/.git:Z,ro")
        return mounts

    @staticmethod
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
        mounts.extend(
            CephBuilder._worktree_submodule_git_mounts(
                repo, worktree_name, git_overlay_dir
            )
        )
        return mounts

    @property
    def config_key(self) -> str:
        return "ceph_builder"

    @property
    def should_build(self) -> bool:
        """Whether to build the builder container image locally."""
        return self.config_bool("build_image")

    @property
    def builder_image_repo(self) -> str | None:
        """Remote registry repo for the builder image (e.g. quay.io/ceph-ci/ceph-build)."""
        return self.config.get("builder_image_repo") or None

    @property
    def builder_image_variant(self) -> str:
        """Builder image variant tag component (e.g. 'default', 'debug')."""
        return str(self.config.get("builder_image_variant", "default"))

    @property
    def target_image(self) -> str:
        """Target image to patch with build artifacts (for binary-patch mode)."""
        return self.config.get(
            "target_image",
            self.config.get("base_image", DEFAULT_CEPH_IMAGE),
        )

    @property
    def build_subdir(self) -> str:
        """Build subdirectory within the repo."""
        build_dir = self.config.get("build_dir", "build")
        if not build_dir:
            return "build"
        path = Path(os.path.expanduser(str(build_dir)))
        if path.is_absolute() and self.repo:
            try:
                return str(path.resolve().relative_to(self.repo))
            except ValueError:
                return path.name
        return str(build_dir).strip("/")

    @property
    def distro(self) -> str:
        if self._resolved_distro is not None:
            return self._resolved_distro
        return self.config.get("build_distro", "centos9")

    @property
    def artifact_type(self):
        if self.image_builder == "package-build":
            return get_package_type(self.distro)
        return "binary"

    @property
    def compile_steps(self) -> List[str]:
        """List of compilation steps to execute."""
        default = DEFAULT_COMPILE_STEPS.get(self.image_builder, ["build"])
        return list(self.config.get("build_steps", default))

    @property
    def persistent_cache_dir(self) -> Path:
        """Directory for persistent build caches."""
        if self._persistent_cache_dir is None:
            self._persistent_cache_dir = self.data_dir / "cache"
        return self._persistent_cache_dir

    @property
    def sccache_enabled(self) -> bool:
        """Whether sccache is enabled."""
        return self.config_bool("sccache", default=True)

    @property
    def sccache_mode(self) -> str:
        """Sccache mode: 'local' or 's3'."""
        return str(self.config.get("sccache_mode", "local")).lower()

    @property
    def sccache_rw_mode(self) -> bool:
        """Whether to use sccache in read-write mode (requires credentials)."""
        return self.config_bool("sccache_rw_mode")

    @property
    def sccache_debug(self) -> bool:
        """Whether to enable sccache debug logging."""
        return self.config_bool("sccache_debug")

    @property
    def sccache_cache_path(self) -> Path:
        """Path to sccache cache directory."""
        if custom := self.config.get("sccache_cache_path"):
            return Path(custom).expanduser().absolute()
        return self.persistent_cache_dir / "sccache"

    @property
    def npm_cache_enabled(self) -> bool:
        """Whether npm cache is enabled."""
        return self.config_bool("npm_cache", default=True)

    @property
    def npm_cache_path(self) -> Path | None:
        """Path to npm cache directory."""
        if not self.npm_cache_enabled:
            return None
        if custom := self.config.get("npm_cache_path"):
            return Path(custom).expanduser().absolute()
        return self.persistent_cache_dir / "npm"

    @property
    def dnf_cache_path(self) -> Path | None:
        """Path to dnf cache directory."""
        if not self.config_bool("dnf_cache"):
            return None
        if custom := self.config.get("dnf_cache_path"):
            return Path(custom).expanduser().absolute()
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
        """Prepare sccache environment variables and extra args."""
        if not self.sccache_enabled:
            return [], []

        homedir = BWC_HOMEDIR
        if custom_conf := self.config.get("sccache_conf"):
            conf_src = Path(custom_conf).expanduser().absolute()
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
        """Prepare build environment file and extra container args."""
        if not self.repo:
            return None, []
        extra_args: list[str] = []

        worktree = self.git_worktree_info(self.repo)
        if worktree is not None:
            main_git_dir, worktree_name = worktree
            extra_args.extend(
                self.worktree_container_mounts(self.repo, main_git_dir, worktree_name)
            )

        lines, sccache_extra = self._sccache_build_env(self.repo)
        extra_args.extend(sccache_extra)
        cmake_extra_args = [
            "-DALLOCATOR=tcmalloc",
            "-DWITH_SYSTEM_BOOST=OFF",
            "-DWITH_BOOST_VALGRIND=ON",
        ]
        if self.sccache_enabled:
            cmake_extra_args.append("-DWITH_SCCACHE=ON")
        lines.append(f"CEPH_EXTRA_CMAKE_ARGS={' '.join(cmake_extra_args)}")
        lines.append("IBM_TELEMETRY_DISABLED=true")
        lines.append("DWZ=false")
        if self.image_builder == "binary-patch":
            lines.append("BUILD_MAKEOPTS=rbd rbd-mirror")

        devstack_dir = self.repo / REPO_DEVSTACK_DIR
        devstack_dir.mkdir(exist_ok=True)

        toolset_script = devstack_dir / GCC_TOOLSET_ENV_NAME
        toolset_script.write_text(GCC_TOOLSET_ENV_SCRIPT, encoding="utf-8")
        lines.append(
            f"BASH_ENV={BWC_HOMEDIR}/{REPO_DEVSTACK_DIR}/{GCC_TOOLSET_ENV_NAME}"
        )

        if not lines:
            return None, extra_args
        env_path = devstack_dir / BUILD_ENV_NAME
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return env_path, extra_args

    @staticmethod
    def _python_cmd() -> str:
        return "python3" if host.type == "remote" else sys.executable

    def _bwc_base_cmd(self) -> list[str]:
        """Common prefix for build-with-container.py commands."""
        assert self.repo is not None
        script = str(self.repo / "src/script/build-with-container.py")
        cmd = [self._python_cmd(), script, "-d", self.distro]
        if self.builder_image_repo:
            cmd.extend(["--image-repo", self.builder_image_repo])
        if not self.should_build:
            tag_suffix = f"+{host_arch()}.{self.builder_image_variant}"
            cmd.extend(["--tag", tag_suffix])
        return cmd

    def _compile_cmd(
        self,
        env_file: Path | None = None,
        extra_args: List[str] | None = None,
        ceph_version: str | None = None,
    ) -> List[str]:
        """Build the compile command for build-with-container.py."""
        assert self.repo is not None
        cmd = [
            *self._bwc_base_cmd(),
            "-b",
            self.build_subdir,
            "--homedir",
            BWC_HOMEDIR,
        ]
        if not self.should_build:
            cmd.extend(["--image-sources", "cache,pull"])
        for step in self.compile_steps:
            cmd.extend(["-e", step])
        if self.image_builder == "package-build":
            if ceph_version is None:
                raise ValueError(
                    "ceph_version is required when image_builder='package-build'"
                )
            cmd.extend(["--ceph-version", ceph_version])
            if self.artifact_type == "rpm":
                cmd.extend(["-R--without=dwz"])
        if env_file is not None:
            cmd.extend(["--env-file", str(env_file)])
        cmd.extend(self._build_cache_args())
        for extra in extra_args or []:
            cmd.append(f"--extra={extra}")
        return cmd

    async def _detect_target_python(self) -> str | None:
        """Return the Python 3 major.minor version from the target image.

        Returns ``None`` (with a warning) when detection fails, e.g. because
        the image has not been pulled or does not contain ``python3``.
        """
        try:
            proc = await self.cmd(
                [
                    "podman",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "python3",
                    self.target_image,
                    "-c",
                    (
                        "import sys;"
                        " print(f'{sys.version_info.major}.{sys.version_info.minor}')"
                    ),
                ],
                check=True,
            )
            stdout, _ = await proc.collect_output()
            return stdout.strip()
        except CalledProcessError:
            logger.warning(
                f"{self.name}: could not detect Python version in target image "
                f"{self.target_image}"
            )
            return None

    async def _git_value(self, args: str) -> str:
        """Execute a git command and return its output."""
        cmd = ["git", *args.split()]
        proc = await self.cmd(cmd, check=True, cwd=self.repo)
        stdout, _ = await proc.collect_output()
        return stdout.strip()

    async def _make_dist_version(self) -> str:
        """Generate version string for make-dist from git describe."""
        version = await self._git_value("describe --abbrev=8 --match v*")
        # Remove leading 'v' from version tag
        if version.startswith("v"):
            version = version[1:]
        return version

    async def _make_dist(self, version: str):
        """Create source distribution tarball using make-dist."""
        if not self.repo:
            return

        # Check if this is a git worktree
        repo_path = self.repo
        git_path = repo_path / ".git"
        if git_path.is_file():
            logger.warning(
                f"{self.name}: Detected git worktree at {repo_path}. "
                "make-dist does not support worktrees (requires .git directory, not file). "
                "Skipping make-dist."
            )
            return

        # Check if tarball already exists (make-dist creates ceph-<version>.tar.bz2)
        tarball_name = f"ceph-{version}.tar.bz2"
        tarball_path = repo_path / tarball_name
        if tarball_path.exists():
            logger.info(
                f"{self.name}: Source tarball {tarball_name} already exists, skipping make-dist"
            )
            return

        logger.info(f"{self.name}: creating source distribution for version {version}")
        make_dist_script = repo_path / "make-dist"
        if not make_dist_script.exists():
            logger.warning(
                f"{self.name}: make-dist script not found at {make_dist_script}, skipping"
            )
            return

        logger.info(
            f"{self.name}: Running make-dist (this may take several minutes "
            "for submodule updates)..."
        )
        await self.cmd(
            ["./make-dist", version],
            cwd=repo_path,
            check=True,
            stream_output=True,
        )
        logger.info(f"{self.name}: make-dist completed successfully")

    async def _resolve_build_distro(self):
        """Auto-select ``build_distro`` to match the target image's Python.

        Only takes effect in binary-patch mode.  When the user has not
        explicitly set ``build_distro``, the resolved value is stored on the
        instance so that all downstream callers (``_bwc_base_cmd``,
        ``_prepare_build_env``, etc.) pick up the correct distro via the
        ``distro`` property.  When the user *has* set it, a mismatch only
        produces a warning.
        """
        if self.image_builder != "binary-patch":
            return
        if self._resolved_distro is not None:
            return
        py_version = await self._detect_target_python()
        if py_version is None:
            return
        recommended = PYTHON_VERSION_DISTROS.get(py_version)
        if recommended is None:
            logger.warning(
                f"{self.name}: target image has Python {py_version} which "
                f"does not map to a known build distro; "
                f"known versions: {', '.join(sorted(PYTHON_VERSION_DISTROS))}"
            )
            return
        configured = self.config.get("build_distro")
        if configured is None:
            if recommended != self.config.get("build_distro", "centos9"):
                logger.info(
                    f"{self.name}: auto-selected build_distro='{recommended}' "
                    f"to match target image Python {py_version}"
                )
            self._resolved_distro = recommended
        else:
            self._resolved_distro = configured
            if configured != recommended:
                logger.warning(
                    f"{self.name}: build_distro='{configured}' has Python "
                    f"{DISTRO_PYTHON_VERSIONS.get(configured, '?')} but the "
                    f"target image uses Python {py_version}; consider setting "
                    f"build_distro='{recommended}'"
                )

    async def compile(self):
        """Run compilation using build-with-container.py."""
        await self._resolve_build_distro()
        ceph_version = None
        if self.image_builder == "package-build":
            ceph_version = await self._make_dist_version()
            await self._make_dist(version=ceph_version)

        logger.info(
            f"{self.name}: compiling ceph via build-with-container.py in {self.repo}"
        )
        env_file, extra_args = self._prepare_build_env()
        await self.cmd(
            self._compile_cmd(
                env_file=env_file,
                extra_args=extra_args,
                ceph_version=ceph_version,
            ),
            cwd=self.repo,
            check=True,
            stream_output=True,
        )

        if self.image_builder == "package-build" and self.artifact_type == "rpm":
            await self._build_runtime_image(ceph_version=ceph_version)
        elif self.image_builder == "binary-patch":
            await self._build_cpatch_image()

    def _runtime_image_name(self) -> str:
        """Local image tag produced after package-build container/build.sh."""
        return runtime_image_tag(self.target_image)

    async def _build_runtime_image(self, ceph_version: str | None = None):
        """Build a Ceph runtime image from locally built RPMs via container/build.sh."""
        assert self.repo is not None
        packages_dir = find_rpm_packages_dir(self.repo, self.build_subdir)

        image_tag = self._runtime_image_name()
        branch = None
        sha1 = None
        try:
            branch = await self._git_value("rev-parse --abbrev-ref HEAD")
            sha1 = await self._git_value("rev-parse HEAD")
        except CalledProcessError:
            logger.warning(
                f"{self.name}: could not resolve git branch/sha for container build"
            )

        build_sh = self.repo / "container" / "build.sh"
        if not build_sh.is_file():
            raise FileNotFoundError(f"container/build.sh not found at {build_sh}")

        async with serve_yum_repo(packages_dir) as repo_url:
            env = build_sh_env(
                custom_ceph_repo_url=repo_url,
                distro=self.distro,
                branch=branch,
                sha1=sha1,
                version=ceph_version,
            )
            logger.info(
                f"{self.name}: building runtime image from {packages_dir} "
                f"via container/build.sh (CUSTOM_CEPH_REPO_URL={repo_url}) "
                f"-> {image_tag}"
            )
            await self.cmd(
                ["bash", str(build_sh)],
                cwd=self.repo / "container",
                env=env,
                check=True,
                stream_output=True,
            )

        tagged = False
        for source in ("build.sh.output", "localhost/build.sh.output"):
            try:
                await self.cmd(
                    ["podman", "tag", source, image_tag],
                    cwd=self.repo,
                    check=True,
                    stream_output=True,
                )
                tagged = True
                break
            except CalledProcessError:
                continue
        if not tagged:
            raise RuntimeError(
                f"container/build.sh finished but neither build.sh.output nor "
                f"localhost/build.sh.output could be tagged as {image_tag}"
            )
        logger.info(
            f"{self.name}: runtime image ready as {image_tag}; "
            f"set containers.ceph_node.image = {image_tag!r} to use it"
        )

    @property
    def cpatch_args(self) -> list[str]:
        """Extra flags passed to cpatch (e.g. --core, --rgw, --py)."""
        return list(self.config.get("cpatch_args", []))

    async def _build_cpatch_image(self):
        """Patch compiled binaries into a base image using cpatch."""
        assert self.repo is not None
        build_path = self.build_path
        image_tag = self.runtime_image_name
        cpatch_script = self.repo / "src" / "script" / "cpatch.py"
        if not cpatch_script.is_file():
            raise FileNotFoundError(f"cpatch script not found at {cpatch_script}")
        logger.info(
            f"{self.name}: patching {self.target_image} -> {image_tag} "
            f"via cpatch in {build_path}"
        )
        await self.cmd(
            [
                self._python_cmd(),
                str(cpatch_script),
                "--base",
                self.target_image,
                "--target",
                image_tag,
                "-b",
                str(build_path),
                *self.cpatch_args,
            ],
            cwd=self.repo,
            check=True,
            stream_output=True,
        )
        repo_name, branch_tag = image_tag.rsplit(":", 1)
        devel_tag = (
            f"{repo_name}:{branch_tag}-{normalize_distro(self.distro)}"
            f"-{host_arch()}-devel"
        )
        await self.cmd(
            ["podman", "tag", image_tag, devel_tag],
            check=True,
        )
        logger.info(f"{self.name}: runtime image ready as {image_tag}")
        logger.info(f"{self.name}: also tagged as {devel_tag}")

    def _has_artifacts(self):
        """Verify that build artifacts exist for the current mode."""
        if self.image_builder == "package-build":
            if any(self.build_path.absolute().glob(f"**/*.{self.artifact_type}")):
                return True
            raise FileNotFoundError(
                f"Ceph build dir {self.build_path} is missing {self.artifact_type} artifacts"
            )
        # binary-patch: look for the ninja build database, which is the
        # concrete output of a successful ``-e build`` step.
        if (self.build_path / "build.ninja").is_file():
            return True
        raise FileNotFoundError(
            f"Ceph build dir {self.build_path} is missing build.ninja"
        )

    async def _runtime_image_exists(self) -> bool:
        """Check whether the runtime container image is already present."""
        proc = await self.cmd(
            ["podman", "image", "exists", self.runtime_image_name],
            check=False,
        )
        return await proc.wait() == 0

    async def exists(self):
        """Check if build artifacts and the runtime image are ready."""
        if not self.repo:
            return False
        try:
            self._has_artifacts()
        except FileNotFoundError:
            return False
        return await self._runtime_image_exists()

    async def pull(self):
        """Pull the builder container image using build-with-container.py."""
        if not self.repo:
            logger.info(f"{self.name}: No repo configured, skipping")
            return

        await self._resolve_build_distro()

        logger.info(f"{self.name}: Pulling builder container image")
        cmd = [
            *self._bwc_base_cmd(),
            "--image-sources",
            "pull",
            "-e",
            "container",
        ]

        await self.cmd(cmd, cwd=self.repo, check=True, stream_output=True)
        logger.info(f"{self.name}: Builder container image pulled")

    async def build(self):
        """Build the builder container image using build-with-container.py."""
        if not self.repo:
            logger.info(f"{self.name}: No repo configured, skipping")
            return

        if not self.should_build:
            logger.info(
                f"{self.name}: build_image is not enabled, skipping builder image build"
            )
            return

        await self._resolve_build_distro()

        logger.info(f"{self.name}: Building builder container image")
        cmd = [
            *self._bwc_base_cmd(),
            "-e",
            "build-container",
        ]

        await self.cmd(cmd, cwd=self.repo, check=True, stream_output=True)
        logger.info(f"{self.name}: Builder container image ready")

    async def create(self):
        """Prepare for compilation (no-op for CephBuilder)."""
        # CephBuilder doesn't need a create step - compilation happens in start()
        pass

    async def start(self):
        """Run build-with-container.py to compile Ceph."""
        if not self.repo:
            logger.warning(f"{self.name}: No repo configured, skipping")
            return

        if await self.exists():
            logger.info(
                f"{self.name}: Artifacts already present at {self.build_path}, "
                "skipping build"
            )
            return

        logger.info(f"{self.name}: Running build-with-container.py to compile Ceph")
        await self.compile()
        self._has_artifacts()
        logger.info(
            f"{self.name}: Compilation complete, artifacts at {self.build_path}"
        )

    async def stop(self):
        pass

    async def remove(self):
        """Clean up build artifacts (preserves caches)."""
        logger.info(
            f"{self.name}: Build caches preserved at {self.persistent_cache_dir}"
        )

    async def is_running(self):
        return False

    async def wait(self):
        return 0


_: Type[StackResource] = CephBuilder
