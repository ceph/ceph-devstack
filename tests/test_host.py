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

    def test_user_ids_returns_os_ids(self, host):
        uid, gid = host.user_ids()
        assert uid == os.getuid()
        assert gid == os.getgid()
        assert isinstance(uid, int)
        assert isinstance(gid, int)


SSH = ["podman", "machine", "ssh", "--"]


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

    @pytest.mark.parametrize(
        "args,expected",
        [
            (["podman", "ps"], ["podman", "ps"]),
            (
                ["sudo", "losetup", "/dev/loop0"],
                SSH + ["sudo", "losetup", "/dev/loop0"],
            ),
            (["bash", "-c", "echo hi; ls"], SSH + ["bash", "-c", "'echo hi; ls'"]),
            (
                ["podman", "unshare", "rm", "/p"],
                SSH + ["podman", "unshare", "rm", "/p"],
            ),
        ],
    )
    def test_cmd_runs_non_podman_commands_in_machine(self, args, expected):
        assert RemoteHost().cmd(args).args == expected

    def test_user_ids_parses_and_caches(self):
        remote = RemoteHost()
        outputs = {"-u": b"501\n", "-g": b"1000\n"}

        def output(args):
            return subprocess.CompletedProcess(args, 0, outputs[args[1]], b"")

        with patch.object(remote, "output", side_effect=output) as mock_output:
            assert remote.user_ids() == (501, 1000)
            assert remote.user_ids() == (501, 1000)
        assert [c.args[0] for c in mock_output.call_args_list] == [
            ["id", "-u"],
            ["id", "-g"],
        ]

    def test_user_ids_raises_on_invalid_output(self):
        remote = RemoteHost()
        bad = subprocess.CompletedProcess([], 0, b"not-a-number\n", b"")
        with (
            patch.object(remote, "output", return_value=bad),
            pytest.raises(RuntimeError, match="'id -u'"),
        ):
            remote.user_ids()
