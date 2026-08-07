"""Serve locally built Ceph RPMs over HTTP for container/build.sh.

Uses upstream ``CUSTOM_CEPH_REPO_URL`` (a URL to a yum ``.repo`` file whose
``baseurl`` points at a createrepo'd RPM tree). The HTTP server is started via
``host.arun`` so on macOS it runs inside the Podman machine — the same place
``podman build`` RUN steps execute.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from ceph_devstack import logger
from ceph_devstack.host import host

DEFAULT_LOCAL_IMAGE = "localhost/ceph-devstack:main"
CUSTOM_REPO_NAME = "custom-ceph.repo"
CREATEREPO_IMAGE = "quay.io/fedora/fedora:40"
# Reachable from podman build RUN containers (not from the host itself).
ADVERTISE_HOST = "host.containers.internal"


def find_rpm_packages_dir(repo: Path, build_subdir: str = "build") -> Path:
    """Locate the directory containing built Ceph RPMs.

    build-with-container.py writes under ``$homedir/rpmbuild`` or
    ``$homedir/$build_dir/rpmbuild`` (homedir is the repo root when mounted at
    ``/ceph``).
    """
    repo = Path(repo).expanduser().absolute()
    candidates = [
        repo / build_subdir / "rpmbuild" / "RPMS",
        repo / "rpmbuild" / "RPMS",
    ]
    for path in candidates:
        if path.is_dir() and any(path.rglob("*.rpm")):
            return path
    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(
        f"No RPMs found under {searched}; package-build must succeed first"
    )


def runtime_image_tag(
    target_image: str,
    branch: str | None = None,
    image_builder: str = "package-build",
) -> str:
    """Pick a local image tag for the runtime image built by ceph_builder."""
    if target_image.startswith("localhost/"):
        return target_image
    suffix = (branch or "main").split("/")[-1] or "main"
    repo_name = "ceph-cpatch" if image_builder == "binary-patch" else "ceph-devstack"
    return f"localhost/{repo_name}:{suffix}"


def from_image_for_distro(distro: str) -> str:
    """Choose a Containerfile FROM_IMAGE compatible with built RPMs."""
    name = distro.lower()
    if name.endswith("9") or "centos9" in name or "rocky9" in name:
        return "quay.io/centos/centos:stream9"
    if name.endswith("10") or "rocky10" in name or "centos10" in name:
        return "docker.io/rockylinux/rockylinux:10"
    return "docker.io/rockylinux/rockylinux:10"


def _reject_loopback_url(url: str, what: str) -> None:
    if "127.0.0.1" in url or "localhost" in url:
        raise ValueError(
            f"{what} must be reachable from podman build containers, "
            f"not loopback: {url!r}"
        )


def write_custom_repo(repo_root: Path, baseurl: str) -> Path:
    """Write a yum ``.repo`` file under ``repo_root`` for CUSTOM_CEPH_REPO_URL."""
    _reject_loopback_url(baseurl, "baseurl")
    repo_root = Path(repo_root).expanduser().absolute()
    repo_root.mkdir(parents=True, exist_ok=True)
    path = repo_root / CUSTOM_REPO_NAME
    # Trailing slash required for yum/dnf baseurl.
    url = baseurl if baseurl.endswith("/") else f"{baseurl}/"
    path.write_text(
        "\n".join(
            [
                "[local-ceph]",
                "name=Local Ceph packages",
                f"baseurl={url}",
                "enabled=1",
                "gpgcheck=0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def build_sh_env(
    *,
    custom_ceph_repo_url: str,
    distro: str,
    branch: str | None = None,
    sha1: str | None = None,
    version: str | None = None,
) -> dict[str, str]:
    """Environment for ``container/build.sh`` with a custom yum repo URL."""
    env = {
        "CUSTOM_CEPH_REPO_URL": custom_ceph_repo_url,
        "NO_PUSH": "true",
        "REMOVE_LOCAL_IMAGES": "false",
        "CI_CONTAINER": "true",
        "FROM_IMAGE": from_image_for_distro(distro),
        "CONTAINER_ENGINE": "podman",
    }
    if branch:
        env["BRANCH"] = branch
    if sha1:
        env["CEPH_SHA1"] = sha1
    if version:
        env["VERSION"] = version
    return env


def createrepo_podman_cmd(packages_dir: Path) -> list[str]:
    """Command to index RPMs via a one-shot createrepo container."""
    packages_dir = Path(packages_dir).expanduser().absolute()
    return [
        "podman",
        "run",
        "--rm",
        "-v",
        f"{packages_dir}:/repo:Z",
        CREATEREPO_IMAGE,
        "bash",
        "-c",
        "dnf install -y -q createrepo_c && createrepo_c /repo",
    ]


async def _host_wait(args: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    proc = await host.arun(args, cwd=cwd)
    returncode = await proc.wait()
    stdout = ""
    if proc.stdout is not None:
        stdout = (await proc.stdout.read()).decode().strip()
    return returncode, stdout


async def ensure_repodata(packages_dir: Path) -> None:
    """Ensure ``packages_dir/repodata`` exists via a one-shot createrepo container."""
    packages_dir = Path(packages_dir).expanduser().absolute()
    if (packages_dir / "repodata").is_dir():
        return

    cmd = createrepo_podman_cmd(packages_dir)
    logger.info(f"Indexing RPM repo via {CREATEREPO_IMAGE} for {packages_dir}")
    # podman is not ssh-wrapped by RemoteHost; volume mount uses shared paths.
    proc = await host.arun(cmd, stream_output=True)
    if await proc.wait() != 0:
        raise RuntimeError(
            f"createrepo via podman failed for {packages_dir}; "
            f"ensure podman can pull {CREATEREPO_IMAGE}"
        )


async def pick_free_port() -> int:
    """Bind port 0 on the build host and return the chosen port."""
    rc, out = await _host_wait(
        [
            "python3",
            "-c",
            (
                "import socket; s=socket.socket(); s.bind(('0.0.0.0', 0)); "
                "print(s.getsockname()[1]); s.close()"
            ),
        ]
    )
    if rc != 0 or not out.isdigit():
        raise RuntimeError(f"failed to allocate free TCP port on build host: {out!r}")
    return int(out)


@asynccontextmanager
async def serve_yum_repo(packages_dir: Path) -> AsyncIterator[str]:
    """Serve ``packages_dir`` over HTTP on the build host; yield CUSTOM_CEPH_REPO_URL.

    The server is started with ``host.arun`` (``python3 -m http.server``), never
    via local_host / macOS-side Python, so macOS Podman machine builds can reach it.
    """
    packages_dir = Path(packages_dir).expanduser().absolute()
    await ensure_repodata(packages_dir)
    port = await pick_free_port()
    baseurl = f"http://{ADVERTISE_HOST}:{port}/"
    write_custom_repo(packages_dir, baseurl)
    repo_url = f"http://{ADVERTISE_HOST}:{port}/{CUSTOM_REPO_NAME}"

    # --directory so cwd need not be honored inside podman machine ssh.
    server_cmd = [
        "python3",
        "-m",
        "http.server",
        str(port),
        "--bind",
        "0.0.0.0",
        "--directory",
        str(packages_dir),
    ]
    logger.info(f"Serving local Ceph RPMs at {repo_url}")
    proc = await host.arun(server_cmd)
    try:
        # Give the server a moment to bind before build.sh curls it.
        await asyncio.sleep(0.3)
        if proc.returncode is not None:
            raise RuntimeError(
                f"HTTP server exited early ({proc.returncode}) for {repo_url}"
            )
        yield repo_url
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.CancelledError):
                    await proc.wait()
