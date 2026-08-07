"""Generic build metadata and image tagging utilities."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path


def _git_output(repo_path: Path, *args: str) -> str | None:
    """Run a git command in *repo_path* and return stripped stdout, or None."""
    repo_path = Path(repo_path).expanduser().absolute()
    if not (repo_path / ".git").exists() and not (repo_path / "HEAD").exists():
        return None
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def git_sha1(repo_path: Path) -> str | None:
    """Resolve the HEAD commit sha1 of a git repo."""
    return _git_output(repo_path, "rev-parse", "HEAD")


def git_branch(repo_path: Path) -> str | None:
    """Resolve the current branch name of a git repo."""
    out = _git_output(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    return None if out == "HEAD" else out


def normalize_distro(distro: str) -> str:
    """Normalize a distro identifier to the container tag format.

    Examples:
        centos9 -> centos-stream9
        centos8 -> centos-stream8
        rocky10 -> rockylinux-10
    """
    import re

    d = distro.lower().strip()
    m = re.match(r"^centos(\d+)$", d)
    if m:
        return f"centos-stream{m.group(1)}"
    m = re.match(r"^rocky(\d+)$", d)
    if m:
        return f"rockylinux-{m.group(1)}"
    return d.replace("/", "-")


def host_arch() -> str:
    """Return the host machine architecture (e.g., x86_64, aarch64)."""
    return platform.machine()


def builder_repo_path() -> Path | None:
    """Resolve the configured ceph_builder repo as an absolute path."""
    from ceph_devstack import config

    repo = config.get("containers", {}).get("ceph_builder", {}).get("repo")
    if not repo:
        return None
    return Path(repo).expanduser().resolve()


def builder_image_tags(distro: str | None = None) -> dict[str, str]:
    """Compute CI-pattern image tags from the ceph_builder repo's git state."""
    repo_path = builder_repo_path()
    if repo_path is None:
        return {}
    if distro is None:
        from ceph_devstack import config

        distro = (
            config.get("containers", {})
            .get("ceph_builder", {})
            .get("build_distro", "centos9")
        )
    return build_image_tags(
        branch=git_branch(repo_path),
        sha1=git_sha1(repo_path),
        distro=distro,
    )


def build_image_tags(
    branch: str | None,
    sha1: str | None,
    distro: str,
    arch: str | None = None,
) -> dict[str, str]:
    """Compute the three CI-pattern image tags.

    Returns a dict with keys "full", "branch", "sha1" mapped to tag strings.
    Only includes tags for which sufficient metadata is available.
    """
    normalized = normalize_distro(distro)
    if arch is None:
        arch = host_arch()

    tags: dict[str, str] = {}
    if branch:
        tags["full"] = f"{branch}-{normalized}-{arch}-devel"
        tags["branch"] = f"{branch}-{normalized}"
    if sha1:
        tags["sha1"] = f"{sha1}-{normalized}"
    return tags
