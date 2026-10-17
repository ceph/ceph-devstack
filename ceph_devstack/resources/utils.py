"""Build host and CI image tag helpers."""

from __future__ import annotations

import platform

from ceph_devstack.distros import normalize_distro


def host_arch() -> str:
    """Return the host machine architecture (e.g., x86_64, aarch64)."""
    return platform.machine()


def devel_tag(branch: str, distro: str, arch: str | None = None) -> str:
    """Return the Ceph CI devel image tag, e.g. main-centos-stream9-x86_64-devel."""
    return f"{branch}-{normalize_distro(distro)}-{arch or host_arch()}-devel"
