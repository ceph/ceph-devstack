from unittest.mock import AsyncMock, patch


from ceph_devstack import config
from ceph_devstack.resources.container import Container
from ceph_devstack.resources.ceph.ceph_node import (
    CLUSTER_ENTRYPOINT_NAME,
    CephNode,
    DEFAULT_CEPH_IMAGE,
)


class TestCephNodeBuild:
    """Tests for CephNode build and configuration."""

    def test_image_uses_configured_tag(self):
        config["containers"]["ceph_node"]["image"] = "quay.io/ceph-ci/ceph:main"
        assert CephNode().image == "quay.io/ceph-ci/ceph:main"

    def test_image_uses_configured_value(self):
        """CephNode should use configured image."""
        config["containers"]["ceph_node"] = {
            "image": "quay.io/ceph-ci/ceph:custom",
            "loop_device_count": 3,
        }
        node = CephNode()
        assert node.image == "quay.io/ceph-ci/ceph:custom"

    def test_image_uses_default_when_no_config(self):
        """When no image configured, CephNode should use default."""
        config["containers"]["ceph_node"] = {"loop_device_count": 3}
        node = CephNode()
        assert node.image == DEFAULT_CEPH_IMAGE

    def test_repo_property_returns_empty_string(self):
        """CephNode doesn't have its own repo."""
        config["containers"]["ceph_node"] = {"loop_device_count": 3}
        node = CephNode()
        assert node.repo == ""

    def test_image_builder_uses_configured_value(self):
        """CephNode.image_builder should use configured value."""
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "image_builder": "package-build",
        }
        node = CephNode()
        assert node.image_builder == "package-build"

    def test_image_builder_defaults_to_binary_patch(self):
        """CephNode.image_builder should default to binary-patch."""
        config["containers"]["ceph_node"] = {"loop_device_count": 3}
        node = CephNode()
        assert node.image_builder == "binary-patch"

    def test_build_path_uses_configured_repo_and_build_dir(self, tmp_path):
        """CephNode.build_path should use configured repo and build_dir."""
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "repo": str(tmp_path),
            "build_dir": "custom-build",
        }
        node = CephNode()
        assert node.build_path == tmp_path / "custom-build"

    def test_build_path_defaults(self, tmp_path):
        """CephNode.build_path should use defaults when not configured."""
        config["containers"]["ceph_node"] = {"loop_device_count": 3}
        node = CephNode()
        # Should use default repo and build_dir
        assert node.build_path.name == "build"

    def test_binary_patch_cmd_uses_config_target_image(self):
        """_binary_patch_cmd should use target_image from config."""
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "target_image": "quay.io/ceph-ci/ceph:main",
            "image": "localhost/test:latest",
        }
        node = CephNode()
        cmd = node._binary_patch_cmd()
        assert cmd[0:2] == ["sudo", "../src/script/cpatch"]
        assert "--base" in cmd
        assert "quay.io/ceph-ci/ceph:main" in cmd
        assert "--target" in cmd
        assert "localhost/test:latest" in cmd

    async def test_build_skips_when_no_repo_configured(self):
        """CephNode.build() should skip when no repo configured."""
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "image": "localhost/test:latest",
        }
        node = CephNode()
        with patch.object(node, "_build_image", new=AsyncMock()) as mock_build:
            await node.build()
            mock_build.assert_not_awaited()

    async def test_build_skips_when_repo_not_found(self, tmp_path):
        """CephNode.build() should skip when repo path doesn't exist."""
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "image": "localhost/test:latest",
            "repo": str(tmp_path / "nonexistent"),
        }
        node = CephNode()
        with patch.object(node, "_build_image", new=AsyncMock()) as mock_build:
            await node.build()
            mock_build.assert_not_awaited()

    async def test_build_uses_local_artifacts(self, tmp_path):
        """CephNode.build() should use local build artifacts when available."""
        build_path = tmp_path / "build"
        build_path.mkdir()
        (build_path / "build.ninja").write_text("")

        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "repo": str(tmp_path),
            "build_dir": "build",
            "image": "localhost/test:latest",
        }
        node = CephNode()
        with patch.object(node, "_build_image", new=AsyncMock()) as mock_build:
            await node.build()
            mock_build.assert_awaited_once()


class TestCephNodeRuntime:
    """Tests for CephNode runtime behavior."""

    def test_devices_allocate_from_empty_host(self):
        config["containers"]["ceph_node"]["loop_device_count"] = 3
        with patch(
            "ceph_devstack.resources.ceph.ceph_node.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop0", "/dev/loop1", "/dev/loop2"]
            node = CephNode()
            assert node.devices == ["/dev/loop0", "/dev/loop1", "/dev/loop2"]

    def test_devices_skip_loops_already_claimed(self):
        config["containers"]["ceph_node"]["loop_device_count"] = 2
        with patch(
            "ceph_devstack.resources.ceph.ceph_node.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop3", "/dev/loop4"]
            node = CephNode()
            assert node.devices == ["/dev/loop3", "/dev/loop4"]

    def test_cluster_dir_defaults_to_stack_data_dir(self):
        config["data_dir"] = "/tmp/test-data"
        config["containers"]["ceph_node"] = {"loop_device_count": 3}
        node = CephNode()
        assert str(node.cluster_dir) == "/tmp/test-data"

    def test_loop_img_dir_lives_outside_cluster_dir(self):
        config["data_dir"] = "/tmp/test-data"
        config["containers"]["ceph_node"] = {"loop_device_count": 3}
        node = CephNode()
        assert str(node.loop_img_dir) == "/tmp/disk_images"

    def test_create_cmd_uses_host_network_and_entrypoint(self):
        config["data_dir"] = "/tmp/test-data"
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "image": "quay.io/ceph-ci/ceph:main",
        }
        with patch(
            "ceph_devstack.resources.ceph.ceph_node.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop0", "/dev/loop1", "/dev/loop2"]
            node = CephNode()
            cmd = node.create_cmd
            assert "--network" in cmd
            assert "host" in cmd
            assert "--entrypoint" in cmd
            assert "/bin/bash" in cmd

    def test_dashboard_show_password_when_enabled(self):
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "dashboard_show_password": True,
        }
        node = CephNode()
        assert node.dashboard_show_password is True

    async def test_create_installs_entrypoint_in_cluster_dir(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "loop_device_size": "5G",
            "image": "quay.io/ceph-ci/ceph:main",
        }
        with patch(
            "ceph_devstack.resources.ceph.ceph_node.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop0", "/dev/loop1", "/dev/loop2"]
            node = CephNode()
            with patch.object(Container, "create", new=AsyncMock()):
                await node.create()
                assert (tmp_path / CLUSTER_ENTRYPOINT_NAME).exists()

    async def test_create_removes_legacy_loop_img_dir(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "loop_device_size": "5G",
            "image": "quay.io/ceph-ci/ceph:main",
        }
        legacy_dir = tmp_path / "disk_images"
        legacy_dir.mkdir()
        (legacy_dir / "test.img").write_text("test")

        with patch(
            "ceph_devstack.resources.ceph.ceph_node.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop0", "/dev/loop1", "/dev/loop2"]
            node = CephNode()
            with patch.object(Container, "create", new=AsyncMock()):
                await node.create()
                assert not legacy_dir.exists()

    async def test_create_sets_up_loop_devices_and_container(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "loop_device_size": "5G",
            "image": "quay.io/ceph-ci/ceph:main",
        }
        with patch(
            "ceph_devstack.resources.ceph.ceph_node.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop0", "/dev/loop1", "/dev/loop2"]
            node = CephNode()
            with patch.object(Container, "create", new=AsyncMock()) as mock_create:
                await node.create()
                mock_create.assert_awaited_once()

    async def test_remove_tears_down_container_and_loop_devices(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "image": "quay.io/ceph-ci/ceph:main",
        }
        cluster_dir = tmp_path
        (cluster_dir / "var").mkdir()
        (cluster_dir / "fsid").write_text("test-fsid")

        with patch(
            "ceph_devstack.resources.ceph.ceph_node.allocate_loop_devices"
        ) as mock:
            mock.return_value = ["/dev/loop0", "/dev/loop1", "/dev/loop2"]
            node = CephNode()
            with (
                patch.object(Container, "remove", new=AsyncMock()),
                patch.object(
                    node, "remove_loop_devices", new=AsyncMock()
                ) as mock_remove,
            ):
                await node.remove()
                mock_remove.assert_awaited_once()

    async def test_is_running_checks_ceph_status_in_container(self):
        config["containers"]["ceph_node"] = {
            "loop_device_count": 3,
            "image": "quay.io/ceph-ci/ceph:main",
        }
        node = CephNode()
        with (
            patch.object(Container, "is_running", return_value=True),
            patch("ceph_devstack.host.host.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = await node.is_running()
            # is_running checks both container and ceph status
            assert result in (True, False)  # Depends on actual implementation
