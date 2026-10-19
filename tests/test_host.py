import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ceph_devstack.host import LocalHost, RemoteHost


class TestLocalHost:
    @pytest.fixture
    def host(self):
        return LocalHost()

    def test_path_exists_expands_tilde(self, host):
        assert host.path_exists("~") is True
        assert host.path_exists("~/nonexistent_dir_12345") is False

    def test_file_helpers_run_commands_on_host(self, host, tmp_path):
        (tmp_path / "a").write_text("hello\n")
        (tmp_path / "link").symlink_to(tmp_path / "a")
        assert host.read_text(tmp_path / "a") == "hello\n"
        assert host.read_text(tmp_path / "missing") is None
        assert host.list_dir(tmp_path) == ["a", "link"]
        assert host.list_dir(tmp_path / "missing") == []
        assert host.realpath(tmp_path / "link") == os.path.realpath(tmp_path / "a")

    def test_byte_io_at_unaligned_offsets(self, host, tmp_path):
        disk = tmp_path / "disk"
        disk.write_bytes(bytes(8192))
        host.write_bytes(disk, 4096 + 256, b"x" * 512)
        data = disk.read_bytes()
        assert len(data) == 8192
        assert data[4352:4864] == b"x" * 512 and data.count(b"x") == 512
        assert host.read_bytes(disk, 4096, 1024) == bytes(256) + b"x" * 512 + bytes(256)

    @pytest.mark.parametrize("stdout,mounted", [(b"/mnt\n", True), (b"", False)])
    def test_device_mounted_asks_findmnt(self, host, stdout, mounted):
        done = subprocess.CompletedProcess([], 0 if stdout else 1, stdout, b"")
        with patch.object(host, "output", return_value=done) as output:
            assert host.device_mounted("/dev/loop3") is mounted
        output.assert_called_once_with(
            ["findmnt", "-n", "-o", "TARGET", "--source", "/dev/loop3"]
        )


class TestRemoteHost:
    def test_write_bytes_pipes_data_through_machine_ssh(self):
        remote = RemoteHost()
        done = MagicMock(returncode=0)
        with patch("ceph_devstack.exec.subprocess.run", return_value=done) as run:
            remote.write_bytes("/dev/sda1", 8192, b"m" * 4096)
        args, kwargs = run.call_args
        assert args[0] == [
            "podman",
            "machine",
            "ssh",
            "--",
            "dd",
            "of=/dev/sda1",
            "bs=4096",
            "seek=2",
            "conv=notrunc,fsync",
        ]
        assert kwargs["input"] == b"m" * 4096
