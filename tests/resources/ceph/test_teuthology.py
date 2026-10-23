from unittest.mock import AsyncMock, patch

import pytest

from ceph_devstack.resources.ceph.containers import Teuthology


class TestTeuthologyCreate:
    @pytest.fixture
    def teuth(self, tmp_path):
        teuth = Teuthology(data_dir=tmp_path)
        teuth.remove = AsyncMock()
        return teuth

    @pytest.fixture
    def podman_create(self):
        with patch(
            "ceph_devstack.resources.container.Container.create", new=AsyncMock()
        ) as podman_create:
            yield podman_create

    @pytest.mark.parametrize(
        "exists,running,replaced",
        [
            (False, False, False),
            (True, True, False),
            (True, False, True),
        ],
        ids=["missing", "running", "stopped"],
    )
    async def test_replaces_only_a_stopped_container(
        self, teuth, podman_create, exists, running, replaced
    ):
        teuth.exists = AsyncMock(return_value=exists)
        teuth.is_running = AsyncMock(return_value=running)
        await teuth.create()
        assert teuth.remove.await_count == int(replaced)
        podman_create.assert_awaited_once()
        assert teuth.archive_dir.is_dir()


class TestTeuthologySSHAuthSock:
    SOCK = "/tmp/ssh-XXXX/agent.123"

    def create_cmd(self, tmp_path, monkeypatch, sock_exists: bool) -> list[str]:
        monkeypatch.setenv("SSH_AUTH_SOCK", self.SOCK)
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists",
            side_effect=lambda path: sock_exists and str(path) == self.SOCK,
        ):
            return Teuthology(data_dir=tmp_path).create_cmd

    def test_mounts_socket_present_on_container_host(self, tmp_path, monkeypatch):
        cmd = self.create_cmd(tmp_path, monkeypatch, sock_exists=True)
        assert f"{self.SOCK}:{self.SOCK}" in cmd
        assert f"SSH_AUTH_SOCK={self.SOCK}" in cmd

    def test_skips_socket_missing_on_container_host(self, tmp_path, monkeypatch):
        cmd = self.create_cmd(tmp_path, monkeypatch, sock_exists=False)
        assert not any("SSH_AUTH_SOCK" in arg or self.SOCK in arg for arg in cmd)
