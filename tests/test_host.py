import pytest
from unittest.mock import MagicMock, patch

from ceph_devstack.host import LocalHost, RemoteHost


class TestLocalHost:
    @pytest.fixture
    def host(self):
        return LocalHost()

    def test_path_exists_expands_tilde(self, host):
        assert host.path_exists("~") is True
        assert host.path_exists("~/nonexistent_dir_12345") is False

    def test_ismount_root(self, host):
        assert host.ismount("/") is True

    def test_ismount_non_mount(self, host, tmp_path):
        assert host.ismount(tmp_path) is False


class TestRemoteHost:
    def test_ismount_uses_remote_mountpoint(self):
        remote = RemoteHost()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        with patch.object(remote, "run", return_value=mock_proc) as mock_run:
            assert remote.ismount("/mnt/data") is True
        mock_run.assert_called_once_with(["mountpoint", "-q", "/mnt/data"])

    def test_ismount_false_when_mountpoint_fails(self):
        remote = RemoteHost()
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        with patch.object(remote, "run", return_value=mock_proc):
            assert remote.ismount("/dev/loop0") is False
