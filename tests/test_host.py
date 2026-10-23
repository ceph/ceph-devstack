import pytest

from ceph_devstack.host import LocalHost, RemoteHost


class TestLocalHost:
    @pytest.fixture
    def host(self):
        return LocalHost()

    def test_path_exists_expands_tilde(self, host):
        assert host.path_exists("~") is True
        assert host.path_exists("~/nonexistent_dir_12345") is False


SSH = ["podman", "machine", "ssh", "--"]


class TestRemoteHost:
    @pytest.mark.parametrize(
        "args,expected",
        [
            (["podman", "ps"], ["podman", "ps"]),
            (
                ["sudo", "losetup", "/dev/loop0"],
                SSH + ["sudo", "losetup", "/dev/loop0"],
            ),
            (["bash", "-c", "echo hi; ls"], SSH + ["bash", "-c", "'echo hi; ls'"]),
        ],
    )
    def test_cmd_runs_non_podman_commands_in_machine(self, args, expected):
        assert RemoteHost().cmd(args).args == expected
