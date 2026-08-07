from pathlib import Path
from unittest.mock import AsyncMock, patch

from ceph_devstack import config
from ceph_devstack.resources.container import Container
from ceph_devstack.resources.ceph.ceph_node import (
    CLUSTER_ENTRYPOINT_NAME,
    CephNode,
)


class TestCephNodeBuild:
    """Tests for CephNode build and configuration."""

    def test_image_uses_configured_value(self, ceph_node_config):
        ceph_node_config(image="quay.ceph.io/ceph-ci/ceph:custom")
        assert CephNode().image == "quay.ceph.io/ceph-ci/ceph:custom"

    def test_image_builder_uses_shipped_config_default(self):
        assert config["containers"]["ceph_node"]["image_builder"] == "binary-patch"
        assert CephNode().image_builder == "binary-patch"

    def test_build_path_uses_configured_repo_and_build_dir(
        self, tmp_path, ceph_node_config
    ):
        ceph_node_config(repo=str(tmp_path), build_dir="custom-build")
        assert CephNode().build_path == tmp_path / "custom-build"

    def test_build_path_empty_without_repo(self, ceph_node_config):
        ceph_node_config()
        assert CephNode().build_path == Path()

    async def test_build_skips_when_image_not_local(self, ceph_node_config):
        ceph_node_config()
        node = CephNode()
        with patch.object(node, "_verify_local_image", new=AsyncMock()) as mock_verify:
            await node.build()
            mock_verify.assert_not_awaited()

    async def test_build_verifies_local_image(self, ceph_node_config):
        ceph_node_config(image="localhost/test:latest")
        node = CephNode()
        with patch.object(node, "_verify_local_image", new=AsyncMock()) as mock_verify:
            await node.build()
            mock_verify.assert_awaited_once()


class TestCephNodeRuntime:
    """Tests for CephNode runtime behavior."""

    def test_devices_allocate_from_empty_host(self, mock_loop_devices):
        config["containers"]["ceph_node"]["loop_device_count"] = 3
        node = CephNode()
        assert node.devices == ["/dev/loop0", "/dev/loop1", "/dev/loop2"]

    def test_devices_skip_loops_already_claimed(self):
        config["containers"]["ceph_node"]["loop_device_count"] = 2
        with patch(
            "ceph_devstack.resources.ceph.host_loops.LoopDeviceMixin.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop3", "/dev/loop4"]
            node = CephNode()
            assert node.devices == ["/dev/loop3", "/dev/loop4"]

    def test_cluster_dir_defaults_to_stack_data_dir(self, ceph_node_config):
        config["data_dir"] = "/tmp/test-data"
        ceph_node_config()
        node = CephNode()
        assert str(node.cluster_dir) == "/tmp/test-data"

    def test_loop_img_dir_lives_outside_cluster_dir(self, ceph_node_config):
        config["data_dir"] = "/tmp/test-data"
        ceph_node_config()
        node = CephNode()
        assert str(node.loop_img_dir) == "/tmp/disk_images"

    def test_create_cmd_uses_host_network_and_entrypoint(
        self, ceph_node_config, mock_loop_devices
    ):
        config["data_dir"] = "/tmp/test-data"
        ceph_node_config()
        node = CephNode()
        cmd = node.create_cmd
        assert "--network" in cmd
        assert "host" in cmd
        assert "--entrypoint" in cmd
        assert "/bin/bash" in cmd

    def test_dashboard_show_password_when_enabled(self, ceph_node_config):
        ceph_node_config(dashboard_show_password=True)
        node = CephNode()
        assert node.dashboard_show_password is True

    async def test_create_installs_entrypoint_in_cluster_dir(
        self, tmp_path, ceph_node_config, mock_loop_devices
    ):
        config["data_dir"] = str(tmp_path)
        ceph_node_config()
        node = CephNode()
        with (
            patch.object(Container, "create", new=AsyncMock()),
            patch.object(node, "create_loop_devices", new=AsyncMock()),
            patch.object(node, "label_cluster_dir", new=AsyncMock()),
            patch.object(node, "remove_legacy_loop_img_dir", new=AsyncMock()),
        ):
            await node.create()
            assert (tmp_path / CLUSTER_ENTRYPOINT_NAME).exists()

    async def test_create_removes_legacy_loop_img_dir(
        self, tmp_path, ceph_node_config, mock_loop_devices
    ):
        config["data_dir"] = str(tmp_path)
        ceph_node_config()
        legacy_dir = tmp_path / "disk_images"
        legacy_dir.mkdir()
        (legacy_dir / "test.img").write_text("test")

        node = CephNode()
        with (
            patch.object(Container, "create", new=AsyncMock()),
            patch.object(node, "create_loop_devices", new=AsyncMock()),
            patch.object(node, "label_cluster_dir", new=AsyncMock()),
            patch.object(node, "cmd", new=AsyncMock()) as mock_cmd,
            patch(
                "ceph_devstack.resources.ceph.ceph_node.host.path_exists",
                return_value=False,
            ),
        ):
            mock_proc = AsyncMock()
            mock_proc.wait = AsyncMock(return_value=1)
            mock_cmd.return_value = mock_proc
            await node.create()
            assert not legacy_dir.exists()

    async def test_create_sets_up_loop_devices_and_container(
        self, tmp_path, ceph_node_config, mock_loop_devices
    ):
        config["data_dir"] = str(tmp_path)
        ceph_node_config()
        node = CephNode()
        with (
            patch.object(Container, "create", new=AsyncMock()) as mock_create,
            patch.object(node, "create_loop_devices", new=AsyncMock()) as mock_loops,
            patch.object(node, "label_cluster_dir", new=AsyncMock()),
            patch.object(node, "remove_legacy_loop_img_dir", new=AsyncMock()),
        ):
            await node.create()
            mock_loops.assert_awaited_once()
            mock_create.assert_awaited_once()

    async def test_remove_tears_down_container_and_loop_devices(
        self, tmp_path, ceph_node_config, mock_loop_devices
    ):
        config["data_dir"] = str(tmp_path)
        ceph_node_config()
        cluster_dir = tmp_path
        (cluster_dir / "var").mkdir()
        (cluster_dir / "fsid").write_text("test-fsid")

        node = CephNode()
        with (
            patch.object(Container, "remove", new=AsyncMock()),
            patch.object(node, "remove_loop_devices", new=AsyncMock()) as mock_remove,
            patch.object(node, "remove_legacy_loop_img_dir", new=AsyncMock()),
            patch.object(node, "remove_cluster_data", new=AsyncMock()),
        ):
            await node.remove()
            mock_remove.assert_awaited_once()

    def test_create_cmd_includes_health_check(
        self, ceph_node_config, mock_loop_devices
    ):
        config["data_dir"] = "/tmp/test-data"
        ceph_node_config()
        node = CephNode()
        cmd = node.create_cmd
        assert "--health-cmd" in cmd
        health_idx = cmd.index("--health-cmd")
        assert "ceph" in cmd[health_idx + 1]
        assert "-s" in cmd[health_idx + 1]
        assert "--health-interval" in cmd
        assert "--health-retries" in cmd
        assert "--health-timeout" in cmd
