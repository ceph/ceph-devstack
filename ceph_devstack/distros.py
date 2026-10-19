"""Distro names: package formats and Ceph CI naming."""

import re

# Default build-with-container.py distro (-d) for the build-ceph stack.
DEFAULT_BUILD_DISTRO = "centos9"
# container/build.sh FROM_IMAGE choices for locally-built RPMs.
CENTOS_STREAM9_IMAGE = "quay.io/centos/centos:stream9"
ROCKY10_IMAGE = "docker.io/rockylinux/rockylinux:10"

package_types = {
    "rpm": ["rhel", "fedora", "centos", "rocky", "alma", "opensuse", "sle"],
    "deb": ["debian", "ubuntu"],
}


def get_package_type(distro: str) -> str:
    for type_, distros in package_types.items():
        if any(distro.startswith(item) for item in distros):
            return type_
    raise ValueError(f"Unknown distro: {distro}")


def normalize_distro(distro: str) -> str:
    """Normalize a distro identifier to the container tag format.

    Examples:
        centos9 -> centos-stream9
        centos8 -> centos-stream8
        rocky10 -> rockylinux-10
    """
    d = distro.lower().strip()
    m = re.match(r"^centos(\d+)$", d)
    if m:
        return f"centos-stream{m.group(1)}"
    m = re.match(r"^rocky(\d+)$", d)
    if m:
        return f"rockylinux-{m.group(1)}"
    return d.replace("/", "-")


def from_image_for_distro(distro: str) -> str:
    """Choose a container/build.sh FROM_IMAGE compatible with built RPMs."""
    if distro.lower().endswith("9"):
        return CENTOS_STREAM9_IMAGE
    return ROCKY10_IMAGE
