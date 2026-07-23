"""CephBuilder resource for managing Ceph compilation and build artifacts."""

import asyncio
import os
import sys
from pathlib import Path
from subprocess import CalledProcessError
from typing import List

import tomlkit

from ceph_devstack import PROJECT_ROOT, config, logger
from ceph_devstack.host import host
from ceph_devstack.resources.container import Container


PACKAGE_SCCACHE_CONF = PROJECT_ROOT / "sccache.conf"
PACKAGE_SCCACHE_S3_CONF = PROJECT_ROOT / "sccache-s3.conf"
CONTAINER_SCCACHE_DIR = "/sccache"
CONTAINER_GIT_METADATA_DIR = "/git-metadata"
BWC_HOMEDIR = "/ceph"
REPO_DEVSTACK_DIR = ".ceph-devstack"
BUILD_ENV_NAME = "build.env"

DEFAULT_COMPILE_STEPS = {
    "binary-patch": ["build"],
    "package-build": ["packages"],
}


def expand_path(path: str | Path) -> Path:
    """Expand user home directory in path."""
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
    import subprocess

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


class CephBuilder(Container):
    """Manages Ceph compilation and build artifacts.

    This resource handles:
    - Builder container image management (via Containerfile.ceph)
    - Compilation via build-with-container.py
    - Build cache management (sccache, npm, dnf)
    - Build artifact production
    """

    _name = "ceph_builder"

    def __init__(self, name: str = ""):
        super().__init__(name)
        self._persistent_cache_dir: Path | None = None

    @property
    def config_key(self) -> str:
        return "ceph_builder"

    @property
    def config(self):
        """Get configuration for this builder."""
        return config["containers"].get(self.config_key, {})

    @property
    def repo(self) -> str:
        """Path to the Ceph repository."""
        return self.config.get("repo", "")

    @property
    def target_image(self) -> str:
        """Target image to patch with build artifacts (for binary-patch mode)."""
        return self.config.get(
            "target_image", self.config.get("base_image", "quay.io/ceph-ci/ceph:main")
        )

    @property
    def build_subdir(self) -> str:
        """Build subdirectory within the repo."""
        build_dir = self.config.get("build_dir", "build")
        if not build_dir:
            return "build"
        path = Path(os.path.expanduser(str(build_dir)))
        if path.is_absolute() and self.repo:
            repo = expand_path(self.repo).resolve()
            try:
                return str(path.resolve().relative_to(repo))
            except ValueError:
                return path.name
        return str(build_dir).strip("/")

    @property
    def build_path(self) -> Path:
        """Full path to the build directory."""
        if not self.repo:
            return Path()
        return expand_path(self.repo) / self.build_subdir

    @property
    def image_builder(self) -> str:
        """Build mode: 'binary-patch' or 'package-build'."""
        return self.config.get("image_builder", "binary-patch")

    @property
    def compile_steps(self) -> List[str]:
        """List of compilation steps to execute."""
        default = DEFAULT_COMPILE_STEPS.get(self.image_builder, ["build"])
        return list(self.config.get("build_steps", default))

    @property
    def persistent_cache_dir(self) -> Path:
        """Directory for persistent build caches."""
        if self._persistent_cache_dir is None:
            data_dir = Path(config.get("data_dir", "~/.local/share/ceph-devstack"))
            self._persistent_cache_dir = expand_path(data_dir) / "cache"
        return self._persistent_cache_dir

    @property
    def sccache_enabled(self) -> bool:
        """Whether sccache is enabled."""
        return self.config.get("sccache", True) is not False

    @property
    def sccache_mode(self) -> str:
        """Sccache mode: 'local' or 's3'."""
        return str(self.config.get("sccache_mode", "local")).lower()

    @property
    def sccache_rw_mode(self) -> bool:
        """Whether to use sccache in read-write mode (requires credentials)."""
        return self.config.get("sccache_rw_mode", False) is True

    @property
    def sccache_debug(self) -> bool:
        """Whether to enable sccache debug logging."""
        return self.config.get("sccache_debug", False) is True

    @property
    def sccache_cache_path(self) -> Path:
        """Path to sccache cache directory."""
        if custom := self.config.get("sccache_cache_path"):
            return expand_path(custom)
        return self.persistent_cache_dir / "sccache"

    @property
    def npm_cache_enabled(self) -> bool:
        """Whether npm cache is enabled."""
        return self.config.get("npm_cache", True) is not False

    @property
    def npm_cache_path(self) -> Path | None:
        """Path to npm cache directory."""
        if not self.npm_cache_enabled:
            return None
        if custom := self.config.get("npm_cache_path"):
            return expand_path(custom)
        return self.persistent_cache_dir / "npm"

    @property
    def dnf_cache_path(self) -> Path | None:
        """Path to dnf cache directory."""
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
        """Prepare build environment file and extra container args."""
        if not self.repo:
            return None, []
        repo = expand_path(self.repo)
        extra_args: list[str] = []

        worktree = git_worktree_info(repo)
        if worktree is not None:
            main_git_dir, worktree_name = worktree
            extra_args.extend(
                worktree_container_mounts(repo, main_git_dir, worktree_name)
            )

        lines, sccache_extra = self._sccache_build_env(repo)
        extra_args.extend(sccache_extra)
        cmake_extra_args = [
            "-DALLOCATOR=tcmalloc",
            "-DWITH_SYSTEM_BOOST=OFF",
            "-DWITH_BOOST_VALGRIND=ON",
        ]
        if self.sccache_enabled:
            cmake_extra_args.append("-DWITH_SCCACHE=ON")
        lines.append(f"CEPH_EXTRA_CMAKE_ARGS={' '.join(cmake_extra_args)}")

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
        """Build the compile command for build-with-container.py."""
        distro = self.config.get("build_distro", "centos9")
        script = str(expand_path(self.repo) / "src/script/build-with-container.py")
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
        if self.image_builder == "package-build":
            # cmd.extend(["--image-variant", "packages"])
            # Pass version to build-with-container.py so make-srpm.sh can find existing tarball
            version = self._make_dist_version()
            cmd.extend(["--ceph-version", version])
        if env_file is not None:
            cmd.extend(["--env-file", str(env_file)])
        cmd.extend(self._build_cache_args())
        for extra in extra_args or []:
            cmd.append(f"--extra={extra}")
        return cmd

    def _git_value(self, args: str) -> str:
        """Execute a git command and return its output."""
        import subprocess

        repo_path = str(expand_path(self.repo))
        logger.debug(f"{self.name}: Running git {args} in {repo_path}")
        try:
            result = subprocess.check_output(
                f"git {args}".split(),
                cwd=repo_path,
                text=True,
                stderr=subprocess.PIPE,
            ).strip()
            logger.debug(f"{self.name}: git {args} returned: {result}")
            return result
        except CalledProcessError as e:
            logger.error(f"{self.name}: git {args} failed in {repo_path}: {e.stderr}")
            raise

    def _make_dist_version(self) -> str:
        """Generate version string for make-dist from git describe."""
        version = self._git_value("describe --abbrev=8 --match v*")
        # Remove leading 'v' from version tag
        if version.startswith("v"):
            version = version[1:]
        return version

    async def _make_dist(self):
        """Create source distribution tarball using make-dist."""
        if not self.repo:
            return

        # Check if this is a git worktree
        repo_path = expand_path(self.repo)
        git_path = repo_path / ".git"
        if git_path.is_file():
            logger.warning(
                f"{self.name}: Detected git worktree at {repo_path}. "
                "make-dist does not support worktrees (requires .git directory, not file). "
                "Skipping make-dist."
            )
            return

        version = self._make_dist_version()

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

        # Run make-dist using asyncio subprocess with streaming output
        logger.info(
            f"{self.name}: Running make-dist (this may take several minutes for submodule updates)..."
        )
        process = await asyncio.create_subprocess_exec(
            "./make-dist",
            version,
            cwd=str(expand_path(self.repo)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream output to show progress
        output_lines = []
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode().rstrip()
            output_lines.append(line_str)
            # Log key progress indicators
            if any(
                keyword in line_str.lower()
                for keyword in ["updating", "synchronizing", "version", "creating"]
            ):
                logger.info(f"{self.name}: {line_str}")

        await process.wait()

        if process.returncode != 0:
            logger.error(f"{self.name}: make-dist failed")
            for line in output_lines[-20:]:  # Show last 20 lines
                logger.error(f"  {line}")
            raise CalledProcessError(
                process.returncode,
                ["./make-dist", version],
                output="\n".join(output_lines),
            )
        logger.info(f"{self.name}: make-dist completed successfully")

    async def _run_cmd(self, cmd: List[str], cwd: str):
        """Run a command and handle errors."""
        proc = await host.arun(
            cmd,
            cwd=expand_path(cwd),
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

    async def compile(self):
        """Run compilation using build-with-container.py."""
        # Run make-dist automatically for package-build mode
        if self.image_builder == "package-build":
            await self._make_dist()

        logger.info(
            f"{self.name}: compiling ceph via build-with-container.py in {self.repo}"
        )
        env_file, extra_args = self._prepare_build_env()
        await self._run_cmd(
            self._compile_cmd(env_file=env_file, extra_args=extra_args),
            cwd=str(self.repo),
        )

    def _verify_build_tree(self):
        """Verify that build artifacts exist."""
        build_path = self.build_path
        if (build_path / "build.ninja").exists() or (build_path / "Makefile").exists():
            return
        raise FileNotFoundError(
            f"Ceph build dir {build_path} missing Makefile or build.ninja "
            "after build-with-container.py"
        )

    async def exists(self):
        """Check if build artifacts exist and are valid."""
        if not self.repo:
            return False
        try:
            self._verify_build_tree()
            return True
        except FileNotFoundError:
            return False

    async def pull(self):
        """Pull the builder container image using build-with-container.py."""
        if not self.repo:
            logger.info(f"{self.name}: No repo configured, skipping")
            return

        logger.info(f"{self.name}: Pulling builder container image")
        # Build minimal command to pull image without compilation
        distro = self.config.get("build_distro", "centos9")
        script = str(expand_path(self.repo) / "src/script/build-with-container.py")
        python_cmd = "python3" if host.type == "remote" else sys.executable

        cmd = [
            python_cmd,
            script,
            "-d",
            distro,
            "--image-sources",
            "pull",
            "-e",
            "container",
        ]

        # if self.image_builder == "package-build":
        #     cmd.extend(["--image-variant", "packages"])

        await self._run_cmd(cmd, cwd=str(self.repo))
        logger.info(f"{self.name}: Builder container image pulled")

    async def build(self):
        """Build the builder container image using build-with-container.py."""
        if not self.repo:
            logger.info(f"{self.name}: No repo configured, skipping")
            return

        logger.info(f"{self.name}: Building builder container image")
        # Use -e build-container to only build the image, not compile
        distro = self.config.get("build_distro", "centos9")
        script = str(expand_path(self.repo) / "src/script/build-with-container.py")
        python_cmd = "python3" if host.type == "remote" else sys.executable

        cmd = [
            python_cmd,
            script,
            "-d",
            distro,
            "-e",
            "build-container",
        ]

        # if self.image_builder == "package-build":
        #     cmd.extend(["--image-variant", "packages"])

        await self._run_cmd(cmd, cwd=str(self.repo))
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

        logger.info(f"{self.name}: Running build-with-container.py to compile Ceph")
        # build-with-container.py orchestrates: starts builder container + compiles
        await self.compile()
        self._verify_build_tree()
        logger.info(
            f"{self.name}: Compilation complete, artifacts at {self.build_path}"
        )

    async def remove(self):
        """Clean up build artifacts (preserves caches)."""
        # Note: We preserve caches by default
        # Users can manually remove cache directories if needed
        logger.info(
            f"{self.name}: Build caches preserved at {self.persistent_cache_dir}"
        )
