"""Tests for ceph_devstack.distros."""

import pytest

from ceph_devstack.distros import (
    DISTRO_PYTHON_VERSIONS,
    PYTHON_VERSION_DISTROS,
    from_image_for_distro,
    get_package_type,
    normalize_distro,
)


class TestGetPackageType:
    @pytest.mark.parametrize(
        "distro, expected",
        [("centos9", "rpm"), ("rocky10", "rpm"), ("ubuntu24.04", "deb")],
    )
    def test_known(self, distro, expected):
        assert get_package_type(distro) == expected

    def test_unknown(self):
        with pytest.raises(ValueError, match="Unknown distro"):
            get_package_type("plan9")


class TestNormalizeDistro:
    @pytest.mark.parametrize(
        "distro, expected",
        [
            ("centos9", "centos-stream9"),
            ("centos8", "centos-stream8"),
            ("rocky10", "rockylinux-10"),
            ("ubuntu24.04", "ubuntu24.04"),
            ("centos/9", "centos-9"),
        ],
    )
    def test_normalize(self, distro, expected):
        assert normalize_distro(distro) == expected


@pytest.mark.parametrize(
    "distro, expected",
    [
        ("centos9", "quay.io/centos/centos:stream9"),
        ("rocky9", "quay.io/centos/centos:stream9"),
        ("centos10", "docker.io/rockylinux/rockylinux:10"),
        ("rocky10", "docker.io/rockylinux/rockylinux:10"),
    ],
)
def test_from_image_for_distro(distro, expected):
    assert from_image_for_distro(distro) == expected


def test_python_version_distros_prefers_first_listed_distro():
    assert PYTHON_VERSION_DISTROS == {"3.9": "centos9", "3.12": "centos10"}
    for python, distro in PYTHON_VERSION_DISTROS.items():
        assert DISTRO_PYTHON_VERSIONS[distro] == python
