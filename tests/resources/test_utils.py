"""Tests for ceph_devstack.resources.utils."""

from ceph_devstack.resources.utils import devel_tag, host_arch


class TestDevelTag:
    def test_explicit_arch(self):
        assert (
            devel_tag("main", "centos9", arch="aarch64")
            == "main-centos-stream9-aarch64-devel"
        )

    def test_defaults_to_host_arch(self):
        assert devel_tag("main", "rocky10") == f"main-rockylinux-10-{host_arch()}-devel"
