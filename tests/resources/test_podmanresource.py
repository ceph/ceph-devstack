import logging
import pytest

from subprocess import CalledProcessError
from unittest.mock import AsyncMock, patch

from ceph_devstack.exec import Subprocess
from ceph_devstack.resources import PodmanResource


class TestPodmanResource:
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        return PodmanResource

    @pytest.fixture(scope="function", params=["create", "remove"])
    def action(self, request):
        return request.param

    def test_name(self, cls):
        obj = cls()
        assert obj.name == cls.__name__.lower()
        obj = cls(name="foo")
        assert obj.name == "foo"

    def test_format_cmd(self, cls):
        obj = cls(name="pr")
        assert "name" in obj.cmd_vars
        res = obj.format_cmd(["foo", "{name}", "bar", "x{name}x"])
        assert res == ["foo", "pr", "bar", "xprx"]

    def test_cmd_vars_contains_name(self, cls):
        assert "name" in cls.cmd_vars

    def test_repr(self, cls):
        obj = cls()
        class_name = cls.__name__
        assert repr(obj) == f"{class_name}()"
        obj = cls(name="foo")
        assert repr(obj) == f'{class_name}(name="foo")'

    async def test_apply(self, cls, action):
        with patch.object(cls, action):
            obj = cls()
            await obj.apply(action)
            method = getattr(obj, action)
            method.assert_awaited_once()

    async def test_cmd(self, cls):
        with patch("ceph_devstack.host.host.arun") as m_arun:
            obj = cls()
            await obj.cmd(["0"])
            m_arun.assert_awaited_once_with(
                ["0"], cwd=".", env=None, stream_output=False
            )

    async def test_cmd_passes_cwd_and_env(self, cls):
        with patch("ceph_devstack.host.host.arun") as m_arun:
            obj = cls()
            await obj.cmd(
                ["make"],
                cwd="/tmp/build",
                env={"FOO": "bar"},
                stream_output=True,
            )
            m_arun.assert_awaited_once_with(
                ["make"],
                cwd="/tmp/build",
                env={"FOO": "bar"},
                stream_output=True,
            )

    async def test_cmd_failed(self, cls, caplog):
        class FakeProc:
            returncode = 1
            collect_output = Subprocess.collect_output
            log_failure = Subprocess.log_failure

            def __init__(self):
                self.stdout = AsyncMock()
                self.stdout.read = AsyncMock(return_value=b"")
                self.stderr = AsyncMock()
                self.stderr.read = AsyncMock(return_value=b"podman-failure\n")

            async def wait(self):
                return 1

        with patch("ceph_devstack.host.host.arun", return_value=FakeProc()) as m_arun:
            obj = cls()
            with caplog.at_level(logging.ERROR), pytest.raises(CalledProcessError):
                await obj.cmd(["podman", "fail"], check=True)
            m_arun.assert_awaited_once()
        assert "Command failed (1)" in caplog.text
        assert "podman-failure" in caplog.text
