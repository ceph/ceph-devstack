"""Tests for ceph_devstack.resources.utils."""

from ceph_devstack.resources.utils import build_image_tags, host_arch, normalize_distro


class TestNormalizeDistro:
    def test_centos9(self):
        assert normalize_distro("centos9") == "centos-stream9"

    def test_centos8(self):
        assert normalize_distro("centos8") == "centos-stream8"

    def test_rocky10(self):
        assert normalize_distro("rocky10") == "rockylinux-10"

    def test_passthrough(self):
        assert normalize_distro("ubuntu24.04") == "ubuntu24.04"

    def test_slash_replaced(self):
        assert normalize_distro("centos/9") == "centos-9"


class TestBuildImageTags:
    def test_all_metadata(self):
        tags = build_image_tags(
            branch="dev-lbc",
            sha1="2314ff99f83a",
            distro="centos9",
            arch="x86_64",
        )
        assert tags["full"] == "dev-lbc-centos-stream9-x86_64-devel"
        assert tags["branch"] == "dev-lbc-centos-stream9"
        assert tags["sha1"] == "2314ff99f83a-centos-stream9"

    def test_no_branch(self):
        tags = build_image_tags(branch=None, sha1="abc123", distro="centos9")
        assert "full" not in tags
        assert "branch" not in tags
        assert tags["sha1"] == "abc123-centos-stream9"

    def test_no_sha1(self):
        tags = build_image_tags(branch="main", sha1=None, distro="centos9")
        assert tags["full"] == f"main-centos-stream9-{host_arch()}-devel"
        assert tags["branch"] == "main-centos-stream9"
        assert "sha1" not in tags

    def test_no_metadata(self):
        tags = build_image_tags(branch=None, sha1=None, distro="centos9")
        assert tags == {}
