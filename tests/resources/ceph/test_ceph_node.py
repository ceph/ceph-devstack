from unittest.mock import AsyncMock, patch
import sys

import pytest
import tomlkit

from ceph_devstack import config
from ceph_devstack.resources.container import Container
from ceph_devstack.resources.ceph.ceph_node import (
    CLUSTER_ENTRYPOINT_NAME,
    CONTAINER_CLUSTER_DIR,
    CephNode,
    ENTRYPOINT_SCRIPT,
)
from ceph_devstack.resources.ceph.ceph_builder import (
    BUILD_ENV_NAME,
    CONTAINER_GIT_METADATA_DIR,
    CONTAINER_SCCACHE_DIR,
    PACKAGE_SCCACHE_CONF,
    PACKAGE_SCCACHE_S3_CONF,
    REPO_DEVSTACK_DIR,
    git_worktree_info,
    worktree_container_mounts,
)


class TestCephNodeBuild:
    """Tests for CephNode build integration with CephBuilder."""
    
    def test_image_uses_configured_tag(self):
        config["containers"]["ceph_node"]["image"] = "quay.io/ceph-ci/ceph:main"
        assert CephNode().image == "quay.io/ceph-ci/ceph:main"

    def test_should_build_requires_repo(self, tmp_path):
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["repo"] = str(tmp_path)
        assert CephNode().should_build is True

    def test_should_build_skips_without_repo(self):
        config["containers"]["ceph_node"]["image"] = "quay.io/ceph-ci/ceph:main"
        assert CephNode().should_build is False

    def test_binary_patch_cmd_uses_builder_base_image(self, tmp_path):
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder
        
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["base_image"] = "quay.io/ceph-ci/ceph:main"
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path)
        config["containers"]["ceph_node"] = {}
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["loop_device_count"] = 3
        
        builder = CephBuilder()
        node = CephNode()
        node.builder = builder
        
        cmd = node._binary_patch_cmd()
        assert cmd[0:2] == ["sudo", "../src/script/cpatch"]
        assert "--base" in cmd
        assert "quay.io/ceph-ci/ceph:main" in cmd
        assert "localhost/ceph-devstack:main" in cmd

    async def test_build_requires_builder_artifacts(self, tmp_path):
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder
        
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path)
        config["containers"]["ceph_node"] = {}
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["loop_device_count"] = 3
        
        builder = CephBuilder()
        node = CephNode()
        node.builder = builder
        
        # Builder has no artifacts yet
        with pytest.raises(RuntimeError, match="Builder has not completed compilation"):
            await node.build()

    async def test_build_uses_builder_artifacts(self, tmp_path):
        from ceph_devstack.resources.ceph.ceph_builder import CephBuilder
        
        build_path = tmp_path / "build"
        build_path.mkdir()
        (build_path / "build.ninja").write_text("")
        
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path)
        config["containers"]["ceph_builder"]["build_dir"] = "build"
        config["containers"]["ceph_node"] = {}
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["loop_device_count"] = 3
        
        builder = CephBuilder()
        node = CephNode()
        node.builder = builder
        
        with patch.object(node, "_build_image", new=AsyncMock()) as mock_build:
            await node.build()
            mock_build.assert_awaited_once()


class TestCephNodeRuntime:
    def test_devices_allocate_from_empty_host(self, tmp_path):
        config["data_dir"] = str(tmp_path / "ceph")
        config["containers"]["ceph_node"]["loop_device_count"] = 3
        assert CephNode().devices == ["/dev/loop0", "/dev/loop1", "/dev/loop2"]

    def test_devices_skip_loops_already_claimed(self, tmp_path):
        config["data_dir"] = str(tmp_path / "ceph")
        image_dir = tmp_path / "disk_images"
        image_dir.mkdir(parents=True)
        (image_dir / "testnode_0-0").write_bytes(b"")
        (image_dir / "testnode_0-1").write_bytes(b"")
        config["containers"]["ceph_node"]["loop_device_count"] = 2
        assert CephNode().devices == ["/dev/loop2", "/dev/loop3"]

    def test_cluster_dir_defaults_to_stack_data_dir(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        assert CephNode().cluster_dir == tmp_path

    def test_loop_img_dir_lives_outside_cluster_dir(self, tmp_path):
        cluster_dir = tmp_path / "ceph"
        config["data_dir"] = str(cluster_dir)
        node = CephNode()
        assert node.loop_img_dir == tmp_path / "disk_images"
        assert node.loop_img_dir != node.cluster_dir / "disk_images"

    def test_create_cmd_uses_host_network_and_entrypoint(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        config["containers"]["ceph_node"]["image"] = "quay.io/ceph-ci/ceph:main"
        cmd = CephNode().create_cmd
        assert "podman" in cmd
        assert "create" in cmd
        assert "--network" in cmd
        assert "host" in cmd
        entrypoint_idx = cmd.index("--entrypoint")
        assert cmd[entrypoint_idx + 1] == "/bin/bash"
        entrypoint = f"{CONTAINER_CLUSTER_DIR}/{CLUSTER_ENTRYPOINT_NAME}"
        assert cmd[cmd.index("-c") + 1] == f". {entrypoint}"
        assert f"{tmp_path}:{CONTAINER_CLUSTER_DIR}" in cmd
        assert f"{tmp_path}/var/lib/ceph:/var/lib/ceph" in cmd
        assert "/run/udev:/run/udev" in cmd
        assert f"CLUSTER_DIR={CONTAINER_CLUSTER_DIR}" in cmd
        assert f"{tmp_path}:{tmp_path}" not in cmd
        assert "CEPH_VOLUME_ALLOW_LOOP_DEVICES" not in cmd
        assert "--device=/dev/loop0" in cmd
        assert "--device=/dev/loop1" in cmd
        assert "--device=/dev/loop2" in cmd
        assert "DASHBOARD_PORT=8080" in cmd
        assert "DASHBOARD_SSL=false" in cmd
        assert "DASHBOARD_SHOW_PASSWORD=false" in cmd
        assert "CONTAINER_NAME=ceph_node" in cmd

    def test_dashboard_show_password_when_enabled(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        config["containers"]["ceph_node"]["image"] = "quay.io/ceph-ci/ceph:main"
        config["containers"]["ceph_node"]["dashboard_show_password"] = True
        assert "DASHBOARD_SHOW_PASSWORD=true" in CephNode().create_cmd

    async def test_create_installs_entrypoint_in_cluster_dir(self, tmp_path):
        cluster_dir = tmp_path / "ceph"
        config["data_dir"] = str(cluster_dir)
        node = CephNode()
        with (
            patch.object(node, "remove_legacy_loop_img_dir", new=AsyncMock()),
            patch.object(node, "label_cluster_dir", new=AsyncMock()),
            patch.object(node, "create_loop_devices", new=AsyncMock()),
            patch.object(Container, "create", new=AsyncMock()),
        ):
            await node.create()
        installed = cluster_dir / CLUSTER_ENTRYPOINT_NAME
        assert installed.is_file()
        assert installed.read_text() == ENTRYPOINT_SCRIPT.read_text()

    async def test_create_removes_legacy_loop_img_dir(self, tmp_path):
        cluster_dir = tmp_path / "ceph"
        cluster_dir.mkdir()
        legacy = cluster_dir / "disk_images"
        legacy.mkdir()
        (legacy / "ceph_node-0").write_bytes(b"")
        config["data_dir"] = str(cluster_dir)
        config["containers"]["ceph_node"]["loop_device_count"] = 1
        node = CephNode()
        with (
            patch.object(node, "create_loop_devices", new=AsyncMock()),
            patch.object(node, "label_cluster_dir", new=AsyncMock()),
            patch.object(Container, "create", new=AsyncMock()),
        ):
            await node.create()
        assert not legacy.exists()

    async def test_create_sets_up_loop_devices_and_container(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        config["containers"]["ceph_node"]["loop_device_count"] = 2
        node = CephNode()
        with (
            patch.object(node, "create_loop_devices", new=AsyncMock()) as mock_loop,
            patch.object(Container, "create", new=AsyncMock()) as mock_super_create,
        ):
            await node.create()
            mock_loop.assert_awaited_once()
            mock_super_create.assert_awaited_once()
            assert (tmp_path / "fsid").exists()

    async def test_remove_tears_down_container_and_loop_devices(self, tmp_path):
        cluster_dir = tmp_path / "ceph"
        cluster_dir.mkdir()
        (cluster_dir / "var" / "lib" / "ceph").mkdir(parents=True)
        (cluster_dir / "fsid").write_text("test\n")
        npm_cache = tmp_path / "cache" / "npm"
        npm_cache.mkdir(parents=True)
        (npm_cache / "marker").write_bytes(b"x" * 100)
        config["data_dir"] = str(cluster_dir)
        node = CephNode()
        with (
            patch.object(Container, "remove", new=AsyncMock()) as mock_super_remove,
            patch.object(node, "remove_loop_devices", new=AsyncMock()) as mock_loop,
            patch.object(node, "remove_legacy_loop_img_dir", new=AsyncMock()),
            patch.object(node, "cmd", new=AsyncMock()) as mock_cmd,
        ):
            await node.remove()
            mock_super_remove.assert_awaited_once()
            mock_loop.assert_awaited_once()
            mock_cmd.assert_any_await(
                [
                    "podman",
                    "unshare",
                    "rm",
                    "-rf",
                    str((cluster_dir / "var").resolve()),
                ],
                check=False,
            )
            mock_cmd.assert_any_await(
                [
                    "podman",
                    "unshare",
                    "rm",
                    "-rf",
                    str((cluster_dir / "fsid").resolve()),
                ],
                check=False,
            )
        assert npm_cache.exists()

    async def test_is_running_checks_ceph_status_in_container(self, tmp_path):
        config["data_dir"] = str(tmp_path)
        node = CephNode()
        with (
            patch.object(node, "exists", new=AsyncMock(return_value=True)),
            patch.object(node, "cmd", new=AsyncMock()) as mock_cmd,
        ):
            mock_cmd.return_value.wait = AsyncMock(return_value=0)
            assert await node.is_running() is True
            assert mock_cmd.await_args is not None
            exec_cmd = mock_cmd.await_args.args[0]
            assert exec_cmd[:3] == ["podman", "exec", "ceph_node"]
            assert "ceph" in exec_cmd
