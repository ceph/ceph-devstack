import hashlib
import pytest
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


SHIPPED = b"shipped module"
SHIPPED_HASH = hashlib.sha256(SHIPPED).hexdigest().encode()
LISTED = b"ceph_devstack\nother_module\n"


def _proc(returncode: int, stdout: bytes) -> AsyncMock:
    proc = AsyncMock()
    proc.stdout.read = AsyncMock(return_value=stdout)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestSELinuxModule:
    @pytest.fixture
    def req(self, tmp_path):
        shipped_pp = tmp_path / "ceph_devstack.pp"
        shipped_pp.write_bytes(SHIPPED)
        with patch.object(SELinuxModule, "SHIPPED_PP", shipped_pp):
            yield SELinuxModule()

    @pytest.mark.parametrize(
        "listed, extract_rc, extract_out, expected",
        [
            pytest.param(b"other_module\n", 0, b"", False, id="not-listed"),
            pytest.param(
                LISTED, 0, SHIPPED_HASH + b"  ceph_devstack.pp\n", True, id="current"
            ),
            pytest.param(
                LISTED, 0, b"0" * 64 + b"  ceph_devstack.pp\n", False, id="stale"
            ),
            # Any failure to read the installed module must not read as
            # "stale", otherwise doctor --fix would reinstall forever.
            pytest.param(LISTED, 1, b"", True, id="extract-failed"),
            pytest.param(
                LISTED,
                0,
                b"Extracting at highest existing priority '400'.\n",
                True,
                id="unparseable",
            ),
        ],
    )
    async def test_check(self, req, listed, extract_rc, extract_out, expected):
        procs = [_proc(0, listed), _proc(extract_rc, extract_out)]
        with patch.object(req.host, "arun", side_effect=procs):
            assert await req.check() is expected

    async def test_check_falls_back_when_extraction_cannot_run(self, req):
        procs = [_proc(0, LISTED), OSError("bash not found")]
        with patch.object(req.host, "arun", side_effect=procs):
            assert await req.check() is True

    async def test_check_without_shipped_module_is_presence_only(self, req):
        req.SHIPPED_PP.unlink()
        with patch.object(req.host, "arun", side_effect=[_proc(0, LISTED)]):
            assert await req.check() is True


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
        devstack.service_specs = {}
        config["containers"] = {
            "custom": {"repo": "/nonexistent/path"},
        }

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
