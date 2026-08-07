package_types = {
    "rpm": ["rhel", "fedora", "centos", "rocky", "alma", "opensuse", "sle"],
    "deb": ["debian", "ubuntu"],
}


def get_package_type(distro: str) -> str:
    for type_, distros in package_types.items():
        if any(distro.startswith(item) for item in distros):
            return type_
    raise ValueError(f"Unknown distro: {distro}")
