from unittest.mock import AsyncMock, MagicMock, patch

from ceph_devstack import config
from ceph_devstack.resources.ceph import CephDevStack

from ceph_devstack.resources.ceph.requirements import (
    HasSudo,
    LoopControlDeviceExists,
    LoopControlDeviceWriteable,
    SELinuxModule,
)


class TestHasSudo:
    def setup_method(self):
        self.req = HasSudo()

    async def test_has_sudo_check_true(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is True

    async def test_has_sudo_check_false(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is False

    def test_has_sudo_check_cmd(self):
        assert self.req.check_cmd == ["sudo", "true"]

    def test_has_sudo_suggest_msg(self):
        assert self.req.suggest_msg == "sudo access is required"


class TestLoopControlDeviceExists:
    def setup_method(self):
        self.req = LoopControlDeviceExists()

    async def test_loop_control_exists_true(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is True

    async def test_loop_control_exists_false(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is False

    def test_loop_control_exists_check_cmd(self):
        assert self.req.check_cmd == ["test", "-e", "/dev/loop-control"]

    def test_loop_control_exists_fix_cmd(self):
        assert self.req.fix_cmd == ["sudo", "modprobe", "loop"]

    def test_loop_control_exists_suggest_msg(self):
        assert self.req.suggest_msg == "/dev/loop-control does not exist"


class TestLoopControlDeviceWriteable:
    def setup_method(self):
        self.req = LoopControlDeviceWriteable()

    async def test_loop_control_writeable_check_true(self):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is True

    async def test_loop_control_writeable_check_false_local(self):
        mock_check_proc = AsyncMock()
        mock_check_proc.wait = AsyncMock(return_value=1)

        mock_stat_proc = MagicMock()
        mock_stat_proc.communicate = MagicMock(return_value=(b"disk", 0))

        mock_whoami_proc = MagicMock()
        mock_whoami_proc.communicate = MagicMock(return_value=(b"testuser", 0))

        async def side_effect_arun(args):
            if "stat" in args:
                return mock_stat_proc
            if "whoami" in args:
                return mock_whoami_proc
            return mock_check_proc

        with (
            patch.object(self.req.host, "arun", side_effect=side_effect_arun),
            patch.object(self.req.host, "type", "local"),
        ):
            result = await self.req.check()
            assert result is False
            assert "usermod" in " ".join(self.req.fix_cmd)

    async def test_loop_control_writeable_check_false_remote(self):
        mock_check_proc = AsyncMock()
        mock_check_proc.wait = AsyncMock(return_value=1)

        mock_stat_proc = MagicMock()
        mock_stat_proc.communicate = MagicMock(return_value=(b"disk", 0))

        mock_whoami_proc = MagicMock()
        mock_whoami_proc.communicate = MagicMock(return_value=(b"testuser", 0))

        async def side_effect_arun(args):
            if "stat" in args:
                return mock_stat_proc
            if "whoami" in args:
                return mock_whoami_proc
            return mock_check_proc

        with (
            patch.object(self.req.host, "arun", side_effect=side_effect_arun),
            patch.object(self.req.host, "type", "remote"),
        ):
            result = await self.req.check()
            assert result is False
            assert "chgrp" in " ".join(self.req.fix_cmd)


class TestSELinuxModule:
    def setup_method(self):
        self.req = SELinuxModule()

    async def test_selinux_module_check_true(self):
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"ceph_devstack\nother_module\n")
        mock_proc.wait = AsyncMock(return_value=0)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is True

    async def test_selinux_module_check_false(self):
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(
            return_value=b"other_module\nanother_module\n"
        )
        mock_proc.wait = AsyncMock(return_value=0)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is False

    async def test_selinux_module_check_empty_output(self):
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        with patch.object(self.req.host, "arun", return_value=mock_proc):
            result = await self.req.check()
            assert result is False


class TestSELinuxModuleFixCmd:
    def test_selinux_module_fix_cmd_local(self):
        class MockLocalHost:
            type = "local"

        with patch.object(
            SELinuxModule,
            "host",
            MockLocalHost(),
        ):
            req = SELinuxModule()
            assert req.fix_cmd[:3] == [
                "sudo",
                "semodule",
                "-i",
            ]
            assert req.fix_cmd[3].endswith("ceph_devstack.pp")

    def test_selinux_module_fix_cmd_remote(self):
        class MockRemoteHost:
            type = "remote"

        with patch.object(
            SELinuxModule,
            "host",
            MockRemoteHost(),
        ):
            req = SELinuxModule()
            assert req.fix_cmd[:7] == [
                "podman",
                "machine",
                "ssh",
                "--",
                "sudo",
                "semodule",
                "-i",
            ]
            assert req.fix_cmd[7].endswith("ceph_devstack.pp")


class TestCephDevStackCheckRequirements:
    async def test_check_requirements_returns_true_when_all_pass(self):
        devstack = CephDevStack()
        devstack.service_specs = {}
        config["containers"] = {}

        with (
            patch("ceph_devstack.resources.ceph.HasSudo") as MockHasSudo,
            patch(
                "ceph_devstack.resources.ceph.LoopControlDeviceExists"
            ) as MockLoopCtrl,
            patch(
                "ceph_devstack.resources.ceph.LoopControlDeviceWriteable"
            ) as MockLoopCtrlWrite,
            patch("ceph_devstack.host.host.selinux_enforcing") as mock_selinux,
        ):
            MockHasSudo.return_value = AsyncMock(evaluate=AsyncMock(return_value=True))
            MockLoopCtrl.return_value = AsyncMock(evaluate=AsyncMock(return_value=True))
            MockLoopCtrlWrite.return_value = AsyncMock(
                evaluate=AsyncMock(return_value=True)
            )
            mock_selinux.return_value = False
            result = await devstack.check_requirements()
            assert result is True

    async def test_check_requirements_returns_false_when_repo_missing(self):
        devstack = CephDevStack()
        config["containers"] = {
            "custom": {"repo": "/nonexistent/path"},
        }
        devstack.service_specs = {"custom": {}}

        with (
            patch("ceph_devstack.resources.ceph.HasSudo") as MockHasSudo,
            patch(
                "ceph_devstack.resources.ceph.LoopControlDeviceExists"
            ) as MockLoopCtrl,
            patch(
                "ceph_devstack.resources.ceph.LoopControlDeviceWriteable"
            ) as MockLoopCtrlWrite,
            patch("ceph_devstack.host.host.selinux_enforcing") as mock_selinux,
            patch("ceph_devstack.host.host.path_exists") as mock_path_exists,
        ):
            MockHasSudo.return_value = AsyncMock(evaluate=AsyncMock(return_value=True))
            MockLoopCtrl.return_value = AsyncMock(evaluate=AsyncMock(return_value=True))
            MockLoopCtrlWrite.return_value = AsyncMock(
                evaluate=AsyncMock(return_value=True)
            )
            mock_selinux.return_value = False
            mock_path_exists.return_value = False
            result = await devstack.check_requirements()
            assert result is False
