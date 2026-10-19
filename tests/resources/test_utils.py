"""Tests for ceph_devstack.resources.utils."""

import logging
import subprocess

from ceph_devstack.resources.utils import devel_tag, git_output, host_arch


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
