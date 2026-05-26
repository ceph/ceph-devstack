import pytest

from unittest.mock import patch, AsyncMock

from ceph_devstack.resources.misc import Network, Secret

from .test_podmanresource import (
    TestPodmanResource as _TestPodmanResource,
)


class TestMiscResource(_TestPodmanResource):
    @pytest.fixture(scope="class", params=[Network, Secret])
    def cls(self, request):
        return request.param

    @pytest.fixture(scope="class", params=["create", "exists", "remove"])
    def action(self, request):
        return request.param

    async def test_exists_means_inspect(self, cls):
        obj = cls()
        assert "inspect" in obj.exists_cmd

    @pytest.mark.parametrize("rc,expected", [[0, True], [1, False]])
    async def test_exists(self, cls, rc, expected):
        obj = cls()
        with patch.object(obj, "cmd") as mock_cmd:
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=rc)
            mock_cmd.return_value = mock_proc
            result = await obj.exists()
            assert result is expected

    @pytest.mark.parametrize("exists", [True, False])
    async def test_create_when_not_exists(self, cls, exists):
        obj = cls()
        with (
            patch.object(obj, "exists", return_value=exists),
            patch.object(obj, "cmd") as mock_cmd,
        ):
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=(0 if exists else 1))
            mock_cmd.return_value = mock_proc
            await obj.create()
            assert len(mock_cmd.call_args_list) == (0 if exists else 1)
