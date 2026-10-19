import contextlib
import subprocess
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ceph_devstack import config
from ceph_devstack.resources.ceph import CephDevStack

from ceph_devstack.resources.ceph.requirements import (
    HasSudo,
    LoopControlDeviceExists,
    LoopControlDeviceWriteable,
    BlockPoolDiskGroup,
    BlockPoolParentAccessible,
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

    @pytest.mark.parametrize(
        "host_type,fix_cmd",
        [
            ("local", ["sudo", "usermod", "-a", "-G", "disk", "testuser"]),
            ("remote", ["sudo", "chgrp", "testuser", "/dev/loop-control"]),
        ],
    )
    async def test_loop_control_writeable_check_false(self, host_type, fix_cmd):
        mock_check_proc = AsyncMock(wait=AsyncMock(return_value=1))
        mock_stat_proc = MagicMock(communicate=MagicMock(return_value=(b"disk", 0)))
        whoami = subprocess.CompletedProcess([], 0, b"testuser\n", b"")
        with (
            patch.object(self.req.host, "arun", return_value=mock_check_proc),
            patch.object(self.req.host, "run", return_value=mock_stat_proc),
            patch.object(self.req.host, "output", return_value=whoami),
            patch.object(self.req.host, "type", host_type),
        ):
            assert await self.req.check() is False
        assert self.req.fix_cmd == fix_cmd


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


class TestBlockPoolRequirements:
    @pytest.fixture
    def configured_pool(self, tmp_path, pool_parent):
        config["block_pool"] = {"parent": pool_parent, "state_dir": str(tmp_path)}
        return pool_parent

    @pytest.mark.parametrize("cls", [BlockPoolDiskGroup, BlockPoolParentAccessible])
    async def test_skipped_when_pool_disabled(self, cls):
        req = cls()
        with patch.object(req.host, "arun") as arun:
            assert await req.check() is True
        arun.assert_not_called()

    async def test_disk_group_member_passes(self, configured_pool):
        req = BlockPoolDiskGroup()
        mock_proc = AsyncMock(wait=AsyncMock(return_value=0))
        with patch.object(req.host, "arun", return_value=mock_proc) as arun:
            assert await req.check() is True
        arun.assert_awaited_once_with(["bash", "-c", "id -nG | grep -qw disk"])

    async def test_disk_group_non_member_gets_fix_cmd(self, configured_pool):
        req = BlockPoolDiskGroup()
        mock_proc = AsyncMock(wait=AsyncMock(return_value=1))
        whoami = subprocess.CompletedProcess([], 0, b"zack\n", b"")
        with (
            patch.object(req.host, "arun", return_value=mock_proc),
            patch.object(req.host, "output", return_value=whoami),
        ):
            assert await req.check() is False
        assert req.fix_cmd == [
            "sudo",
            "bash",
            "-c",
            "grep -q '^disk:' /etc/group || grep '^disk:' /usr/lib/group"
            " >> /etc/group; usermod -a -G disk zack",
        ]

    @pytest.mark.parametrize("fix", [False, True])
    @pytest.mark.parametrize("cls", [BlockPoolDiskGroup, BlockPoolParentAccessible])
    async def test_invalid_pool_config_fails_without_traceback(self, cls, fix, caplog):
        config["block_pool"] = {"parent": "/dev/dm-0"}
        req = cls()
        with (
            patch.dict(config, {"args": {"fix": fix}}),
            patch.object(req.host, "arun") as arun,
        ):
            assert await req.evaluate() is False
        arun.assert_not_called()
        assert "not allowed" in caplog.text

    @pytest.mark.parametrize("returncode,expected", [(0, True), (1, False)])
    async def test_parent_access_checked_on_host(
        self, configured_pool, returncode, expected
    ):
        req = BlockPoolParentAccessible()
        mock_proc = AsyncMock(wait=AsyncMock(return_value=returncode))
        with patch.object(req.host, "arun", return_value=mock_proc) as arun:
            assert await req.check() is expected
        arun.assert_awaited_once_with(
            ["test", "-r", configured_pool, "-a", "-w", configured_pool]
        )


CORE_REQUIREMENTS = [
    "HasSudo",
    "LoopControlDeviceExists",
    "LoopControlDeviceWriteable",
    "BlockPoolDiskGroup",
    "BlockPoolParentAccessible",
]


@pytest.fixture
def requirement_results():
    """Stub CephDevStack's requirement classes; set an entry False to fail it."""
    results = dict.fromkeys(CORE_REQUIREMENTS, True)
    with contextlib.ExitStack() as stack:
        for name in CORE_REQUIREMENTS:
            mock = stack.enter_context(patch(f"ceph_devstack.resources.ceph.{name}"))
            mock.return_value.evaluate = AsyncMock(
                side_effect=lambda name=name: results[name]
            )
        stack.enter_context(
            patch("ceph_devstack.host.host.selinux_enforcing", return_value=False)
        )
        yield results


class TestCephDevStackCheckRequirements:
    @pytest.mark.parametrize("failing", [None, *CORE_REQUIREMENTS])
    async def test_check_requirements_fails_if_any_requirement_fails(
        self, requirement_results, failing
    ):
        devstack = CephDevStack()
        devstack.service_specs = {"testnode": {}}
        config["containers"] = {"testnode": {}}
        if failing:
            requirement_results[failing] = False
        assert await devstack.check_requirements() is (failing is None)

    async def test_check_requirements_returns_false_when_repo_missing(
        self, requirement_results
    ):
        devstack = CephDevStack()
        devstack.containers_config = {"custom": {"repo": "/nonexistent/path"}}
        devstack.service_specs = {"custom": {}}
        with patch("ceph_devstack.host.host.path_exists", return_value=False):
            assert await devstack.check_requirements() is False
