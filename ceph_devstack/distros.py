"""Distro names: package formats and Ceph CI naming."""

import re

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
