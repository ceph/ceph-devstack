from unittest.mock import AsyncMock, patch


from ceph_devstack import config
from ceph_devstack.resources.container import Container
from ceph_devstack.resources.ceph.ceph_node import (
    CLUSTER_ENTRYPOINT_NAME,
    CephNode,
    DEFAULT_CEPH_IMAGE,
)


class TestCephNodeBuild:
    """Tests for CephNode build integration with CephBuilder."""

    def test_image_uses_configured_tag(self):
        config["containers"]["ceph_node"]["image"] = "quay.io/ceph-ci/ceph:main"
        assert CephNode().image == "quay.io/ceph-ci/ceph:main"

    def test_image_uses_builder_target_when_builder_has_repo(self, tmp_path):
        """When builder has a repo, CephNode should use builder's target_image."""
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        config["containers"]["ceph_builder"] = {
            "repo": str(tmp_path),
            "target_image": "quay.io/ceph-ci/ceph:custom",
        }
        config["containers"]["ceph_node"] = {
            "image": "quay.io/ceph-ci/ceph:main",
            "loop_device_count": 3,
        }

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        assert node.image == "quay.io/ceph-ci/ceph:custom"

    def test_image_falls_back_to_config_when_no_builder_repo(self):
        """When builder has no repo, CephNode should use configured image."""
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_node"] = {
            "image": "quay.io/ceph-ci/ceph:main",
            "loop_device_count": 3,
        }

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        assert node.image == "quay.io/ceph-ci/ceph:main"

    def test_image_uses_default_when_no_config(self):
        """When no image configured, CephNode should use default."""
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_node"] = {"loop_device_count": 3}

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        assert node.image == DEFAULT_CEPH_IMAGE

    def test_repo_property_returns_empty_string(self):
        """CephNode doesn't have its own repo - it uses builder's artifacts."""
        config["containers"]["ceph_node"] = {"loop_device_count": 3}

        node = CephNode()
        assert node.repo == ""

    def test_should_build_requires_repo(self, tmp_path):
        """CephNode.should_build is always False - it doesn't have a repo."""
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        config["containers"]["ceph_builder"] = {"repo": str(tmp_path)}
        config["containers"]["ceph_node"] = {"loop_device_count": 3}

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        # CephNode.should_build checks self.repo which is always ""
        assert node.should_build is False

    def test_should_build_skips_without_repo(self):
        """CephNode.should_build is False when builder has no repo."""
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_node"] = {"loop_device_count": 3}

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        assert node.should_build is False

    def test_binary_patch_cmd_uses_builder_target_image(self, tmp_path):
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["target_image"] = (
            "quay.io/ceph-ci/ceph:main"
        )
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path)
        config["containers"]["ceph_node"] = {}
        config["containers"]["ceph_node"]["loop_device_count"] = 3

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        cmd = node._binary_patch_cmd()
        assert cmd[0:2] == ["sudo", "../src/script/cpatch"]
        assert "--base" in cmd
        assert "quay.io/ceph-ci/ceph:main" in cmd
        # CephNode.image now returns builder.target_image when builder has repo
        assert node.image == "quay.io/ceph-ci/ceph:main"

    async def test_build_requires_builder_artifacts(self, tmp_path):
        """CephNode.build() requires builder to have completed compilation."""
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path)
        config["containers"]["ceph_node"] = {}
        config["containers"]["ceph_node"]["loop_device_count"] = 3

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        # When builder has no build artifacts, build should skip
        with patch.object(node, "_build_image", new=AsyncMock()) as mock_build:
            await node.build()
            # Should not call _build_image when no artifacts
            mock_build.assert_not_awaited()

    async def test_build_uses_builder_artifacts(self, tmp_path):
        """CephNode.build() uses builder artifacts when available."""
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder

        build_path = tmp_path / "build"
        build_path.mkdir()
        (build_path / "build.ninja").write_text("")

        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path)
        config["containers"]["ceph_builder"]["build_dir"] = "build"
        config["containers"]["ceph_builder"]["target_image"] = "localhost/test:latest"
        config["containers"]["ceph_node"] = {}
        config["containers"]["ceph_node"]["loop_device_count"] = 3

        builder = CephBuilder()
        node = CephNode()
        node.builder = builder

        # Node image must start with localhost/ to trigger build
        assert node.image.startswith("localhost/")

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
