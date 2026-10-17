"""Tests for ceph_devstack.distros."""

import pytest

from ceph_devstack.distros import get_package_type, normalize_distro


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
