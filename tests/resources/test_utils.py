"""Tests for ceph_devstack.resources.utils."""

import logging
import subprocess

from unittest.mock import patch

import pytest

from ceph_devstack.resources.utils import (
    build_image_tags,
    devel_tag,
    git_branch,
    git_output,
    git_sha1,
    host_arch,
)


class TestDevelTag:
    def test_explicit_arch(self):
        assert (
            devel_tag("main", "centos9", arch="aarch64")
            == "main-centos-stream9-aarch64-devel"
        )

    def test_defaults_to_host_arch(self):
        assert devel_tag("main", "rocky10") == f"main-rockylinux-10-{host_arch()}-devel"


class TestGitOutput:
    def test_none_outside_a_checkout(self, tmp_path):
        assert git_output(tmp_path, "rev-parse", "HEAD") is None

    def test_returns_stripped_stdout(self, tmp_path):
        subprocess.run(["git", "init", "-q", "-b", "trunk", str(tmp_path)], check=True)
        out = git_output(tmp_path, "symbolic-ref", "--short", "HEAD")
        assert out == "trunk"

    def test_none_when_command_fails(self, tmp_path, caplog):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        with caplog.at_level(logging.DEBUG):
            assert git_output(tmp_path, "rev-parse", "--verify", "nonexistent") is None
        assert "git rev-parse --verify nonexistent failed" in caplog.text


class TestGitMetadata:
    @patch("ceph_devstack.resources.utils.git_output", return_value="HEAD")
    def test_detached_head_has_no_branch(self, _mock, tmp_path):
        assert git_branch(tmp_path) is None

    @patch("ceph_devstack.resources.utils.git_output", return_value="main")
    def test_branch(self, _mock, tmp_path):
        assert git_branch(tmp_path) == "main"

    @patch("ceph_devstack.resources.utils.git_output", return_value=None)
    def test_no_metadata_outside_a_checkout(self, _mock, tmp_path):
        assert git_branch(tmp_path) is None
        assert git_sha1(tmp_path) is None


@pytest.mark.parametrize(
    "branch, sha1, expected",
    [
        (
            "dev-lbc",
            "2314ff99f83a",
            {
                "full": "dev-lbc-centos-stream9-x86_64-devel",
                "branch": "dev-lbc-centos-stream9",
                "sha1": "2314ff99f83a-centos-stream9",
            },
        ),
        (None, "abc123", {"sha1": "abc123-centos-stream9"}),
        (
            "main",
            None,
            {
                "full": "main-centos-stream9-x86_64-devel",
                "branch": "main-centos-stream9",
            },
        ),
        (None, None, {}),
    ],
)
def test_build_image_tags(branch, sha1, expected):
    tags = build_image_tags(branch=branch, sha1=sha1, distro="centos9", arch="x86_64")
    assert tags == expected
