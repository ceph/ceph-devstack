import json
import os
import pytest

from pathlib import Path
from unittest.mock import patch, AsyncMock

from ceph_devstack import config
from ceph_devstack.resources.container import Container
from .test_podmanresource import (
    TestPodmanResource as _TestPodmanResource,
)


class _TestContainerBase:
    @pytest.fixture
    def cls(self):
        return Container

    def setup_method(self):
        config["containers"]["container"] = {"image": "example.com/image:latest"}


class TestContainerResource(_TestPodmanResource, _TestContainerBase):
    @pytest.fixture
    def cls(self):
        return Container

    @pytest.fixture(
        scope="function", params=["build", "create", "start", "stop", "remove"]
    )
    def action(self, request):
        return request.param

    async def test_action_calls_cmd_with_correct_args(self, cls, action):
        if action == "build":
            config["containers"][cls.__name__.lower()]["repo"] = "/repo_path"
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc = AsyncMock()
            mock_cmd.return_value = mock_proc
            action_cmd = "rm" if action == "remove" else action
            await getattr(obj, action)()
            if action == "create":
                assert len(mock_cmd.call_args_list) == 2
                assert "inspect" in mock_cmd.call_args_list[0][0][0]
                assert action_cmd in mock_cmd.call_args_list[-1][0][0]
            else:
                mock_cmd.assert_called_once()
                call_args = mock_cmd.call_args[0][0]
                assert action_cmd in call_args

    async def test_empty_cmd_skips_action(self, cls, action):
        with patch.object(cls, "cmd"):
            obj = cls()
            setattr(obj, f"{action}_cmd", [])
            await getattr(obj, action)()
            obj.cmd.assert_not_awaited()

    async def test_action_cmd_called_with_stream_output(self, cls, action):
        if action == "remove":
            pytest.skip("remove action doesn't stream output")
        if action == "build":
            config["containers"][cls.__name__.lower()]["repo"] = "/repo_path"
        with patch.object(cls, "cmd") as mock_cmd:
            obj = cls()
            await getattr(obj, action)()
            _, kwargs = mock_cmd.call_args
            assert kwargs.get("stream_output") is True

    async def test_build_action_skips_when_no_repo(self, cls):
        config["containers"][cls.__name__.lower()]["repo"] = ""
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            await obj.build()
            mock_cmd.assert_not_called()

    async def test_pull_action_skips_localhost_images(self, cls):
        config["containers"]["container"]["image"] = "localhost/image:latest"
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            await obj.pull()
            mock_cmd.assert_not_called()

    @pytest.mark.parametrize(
        "output,rc,expected", ([b"12345", 0, 12345], [b"error", 1, 1])
    )
    async def test_wait_returns_output_on_success(self, cls, output, rc, expected):
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(output, b""))
            mock_proc.returncode = rc
            mock_cmd.return_value = mock_proc
            result = await obj.wait()
            assert expected == result

    async def test_wait_action_returns_error_code_on_failure(self, cls):
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error occurred"))
            mock_proc.returncode = 130
            mock_cmd.return_value = mock_proc
            result = await obj.wait()
            assert result == 130


class TestContainerInit(_TestContainerBase):
    def test_init_sets_env_vars_from_class(self, cls):
        Container.env_vars = {"TEST_VAR": "default_value"}
        obj = cls()
        assert "TEST_VAR" in obj.env_vars
        assert obj.env_vars["TEST_VAR"] == "default_value"

    def test_init_overrides_env_vars_from_environment(self, cls):
        Container.env_vars = {"TEST_VAR": "default"}
        with patch.dict(os.environ, {"TEST_VAR": "env_value"}):
            obj = cls()
        assert obj.env_vars["TEST_VAR"] == "env_value"

    def test_init_does_not_override_missing_env_vars(self, cls):
        Container.env_vars = {"TEST_VAR": "default"}
        with patch.dict(os.environ, {}, clear=False):
            if "TEST_VAR" in os.environ:
                del os.environ["TEST_VAR"]
            obj = cls()
        assert obj.env_vars["TEST_VAR"] == "default"


class TestContainerExists(_TestContainerBase):
    @pytest.mark.parametrize("rc,res", ([0, True], [1, False]))
    async def test_exists(self, cls, rc, res):
        with patch.object(cls, "cmd"):
            obj = cls()
            obj.cmd.return_value = AsyncMock()
            obj.cmd.return_value.wait.return_value = rc
            assert await obj.exists() == res


class TestContainerRunning(_TestContainerBase):
    async def test_is_running_yes(self, cls):
        with patch.object(cls, "cmd"):
            obj = cls()
            output_obj = [{"State": {"Status": "running"}}]
            m_read = AsyncMock(return_value=json.dumps(output_obj))
            m_stdout = AsyncMock(read=m_read)
            obj.cmd.return_value = AsyncMock(
                stdout=m_stdout,
                returncode=0,
            )
            obj.cmd.return_value.wait.return_value = 0
            assert await obj.is_running() is True

    async def test_is_running_no_bc_status(self, cls):
        with patch.object(cls, "cmd"):
            obj = cls()
            output_obj = [{"State": {"Status": "crashed"}}]
            m_read = AsyncMock(return_value=json.dumps(output_obj))
            m_stdout = AsyncMock(read=m_read)
            obj.cmd.return_value = AsyncMock(
                stdout=m_stdout,
                returncode=0,
            )
            obj.cmd.return_value.wait.return_value = 0
            assert await obj.is_running() is False

    async def test_is_running_no_bc_dne(self, cls):
        with patch.object(cls, "cmd"):
            obj = cls()
            obj.cmd.return_value = AsyncMock(returncode=1)
            assert await obj.is_running() is False


class TestContainerImageName(_TestContainerBase):
    def test_image_name_default_returns_class_name(self, cls):
        obj = cls()
        assert obj.image_name == "container"

    def test_image_name_returns_custom_when_set(self, cls):
        obj = cls()
        obj._image_name = "custom-image"
        assert obj.image_name == "custom-image"


class TestContainerImageTag(_TestContainerBase):
    def test_image_tag_with_colon(self, cls):
        obj = cls()
        config["containers"]["container"] = {"image": "example.com/image:v1.0"}
        assert obj.image_tag == "v1.0"

    def test_image_tag_without_colon(self, cls):
        obj = cls()
        config["containers"]["container"] = {"image": "example.com/image"}
        assert obj.image_tag == "latest"


class TestContainerImage(_TestContainerBase):
    def test_image_returns_config_image_when_no_repo(self, cls):
        obj = cls()
        assert obj.image == "example.com/image:latest"

    def test_image_returns_localhost_when_repo_exists(self, cls):
        config["containers"]["container"]["repo"] = "/path/to/repo"
        obj = cls()
        obj._image_name = "my-image"
        assert obj.image == "localhost/my-image"


class TestContainerCwd(_TestContainerBase):
    def test_cwd_returns_repo_when_exists(self, cls):
        obj = cls()
        with patch.object(type(obj), "repo", Path("/path/to/repo")):
            assert obj.cwd == Path("/path/to/repo")

    def test_cwd_returns_dot_when_no_repo(self, cls):
        obj = cls()
        assert obj.cwd == "."


class TestContainerAddEnvToArgs(_TestContainerBase):
    def test_add_env_to_args_inserts_env_vars(self, cls):
        obj = cls()
        obj.env_vars = {"KEY1": "value1", "KEY2": "value2"}
        args = ["podman", "run", "image"]
        result = obj.add_env_to_args(args)
        assert result[-1] == "image"  # last element is preserved
        assert "-e" in result
        assert "KEY1=value1" in result
        assert "KEY2=value2" in result

    def test_add_env_to_args_skips_empty_values(self, cls):
        obj = cls()
        obj.env_vars = {"KEY1": "value1", "KEY2": None, "KEY3": ""}
        args = ["podman", "run", "image"]
        result = obj.add_env_to_args(args)
        assert "KEY1=value1" in result
        assert "KEY2=" not in result
        assert "KEY3=" not in result

    def test_add_env_to_args_preserves_order(self, cls):
        obj = cls()
        obj.env_vars = {"KEY": "value"}
        args = ["podman", "run", "image"]
        result = obj.add_env_to_args(args)
        assert result[-1] == "image"
        assert result.index("-e") < result.index("image")
