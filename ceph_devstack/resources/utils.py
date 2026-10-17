"""Build host, git and CI image tag helpers."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from ceph_devstack import logger
from ceph_devstack.distros import normalize_distro


def git_output(repo_path: Path, *args: str) -> str | None:
    """Run ``git *args`` in *repo_path*; return stripped stdout, or None.

    Returns None when *repo_path* is not a git checkout (so git never
    walks up into an enclosing repo) or when the command fails.
    """
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
    except subprocess.CalledProcessError as e:
        logger.debug(f"git {' '.join(args)} failed in {repo_path}: {e.stderr.strip()}")
        return None
    except FileNotFoundError as e:
        logger.debug(f"git {' '.join(args)} could not run in {repo_path}: {e}")
        return None
    return result.stdout.strip()


def git_sha1(repo_path: Path) -> str | None:
    """Resolve the HEAD commit sha1 of a git repo."""
    return git_output(repo_path, "rev-parse", "HEAD") or None


def git_branch(repo_path: Path) -> str | None:
    """Resolve the current branch name of a git repo; None when detached."""
    out = git_output(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
    return None if out in (None, "", "HEAD") else out


def host_arch() -> str:
    """Return the host machine architecture (e.g., x86_64, aarch64)."""
    return platform.machine()


def devel_tag(branch: str, distro: str, arch: str | None = None) -> str:
    """Return the Ceph CI devel image tag, e.g. main-centos-stream9-x86_64-devel."""
    return f"{branch}-{normalize_distro(distro)}-{arch or host_arch()}-devel"


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
    tags: dict[str, str] = {}
    if branch:
        tags["full"] = devel_tag(branch, distro, arch)
        tags["branch"] = f"{branch}-{normalized}"
    if sha1:
        tags["sha1"] = f"{sha1}-{normalized}"
    return tags
