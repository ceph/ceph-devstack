from unittest.mock import AsyncMock, patch
import sys


from ceph_devstack import config
from ceph_devstack.resources.container import Container
from ceph_devstack.resources.ceph.ceph_node import (
    BUILD_ENV_NAME,
    CLUSTER_ENTRYPOINT_NAME,
    CONTAINER_CLUSTER_DIR,
    CONTAINER_GIT_METADATA_DIR,
    CONTAINER_SCCACHE_DIR,
    CephNode,
    ENTRYPOINT_SCRIPT,
    PACKAGE_SCCACHE_CONF,
    PACKAGE_SCCACHE_S3_CONF,
    REPO_DEVSTACK_DIR,
    git_worktree_info,
    worktree_container_mounts,
)


class TestCephNodeBuild:
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

    def test_compile_steps_default_for_cpatch(self):
        config["containers"]["ceph_node"]["image"] = "example:test"
        assert CephNode().compile_steps == ["build"]

    def test_compile_cmd_uses_build_with_container(self, tmp_path):
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["repo"] = str(tmp_path)
        config["containers"]["ceph_node"]["build_dir"] = "build"
        config["containers"]["ceph_node"]["build_distro"] = "centos9"
        config["containers"]["ceph_node"]["sccache"] = False
        config["containers"]["ceph_node"]["npm_cache"] = False
        cmd = CephNode()._compile_cmd()
        assert cmd[0] == sys.executable
        assert "build-with-container.py" in cmd[1]
        assert "-d" in cmd and "centos9" in cmd
        assert "-b" in cmd and "build" in cmd
        assert cmd[cmd.index("--homedir") + 1] == "/ceph"
        assert "--env-file" not in cmd
        assert "--npm-cache-path" not in cmd

    def test_compile_cmd_passes_npm_cache_path(self, tmp_path):
        npm_cache = tmp_path / "npm-cache"
        config["data_dir"] = str(tmp_path / "data")
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_node"]["npm_cache_path"] = str(npm_cache)
        config["containers"]["ceph_node"]["sccache"] = False
        cmd = CephNode()._compile_cmd()
        assert "--npm-cache-path" in cmd
        assert str(npm_cache.resolve()) in cmd
        assert npm_cache.is_dir()

    def test_compile_cmd_uses_default_npm_cache_under_data_dir(self, tmp_path):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_node"]["sccache"] = False
        cmd = CephNode()._compile_cmd()
        expected = (data_dir.parent / "cache" / "npm").resolve()
        assert "--npm-cache-path" in cmd
        assert str(expected) in cmd
        assert expected.is_dir()

    def test_compile_cmd_skips_ccache_dir(self, tmp_path):
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_node"]["npm_cache"] = False
        cmd = CephNode()._compile_cmd()
        assert "--ccache-dir" not in cmd

    def test_compile_cmd_passes_dnf_cache_path_when_enabled(self, tmp_path):
        dnf_cache = tmp_path / "dnf-cache"
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_node"]["npm_cache"] = False
        config["containers"]["ceph_node"]["sccache"] = False
        config["containers"]["ceph_node"]["dnf_cache"] = True
        config["containers"]["ceph_node"]["dnf_cache_path"] = str(dnf_cache)
        cmd = CephNode()._compile_cmd()
        assert "--dnf-cache-path" in cmd
        assert str(dnf_cache.resolve()) in cmd
        assert dnf_cache.is_dir()

    def test_prepare_build_env_uses_local_sccache_by_default(self, tmp_path):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(repo)
        env_file, extra_args = CephNode()._prepare_build_env()
        assert env_file == repo / REPO_DEVSTACK_DIR / BUILD_ENV_NAME
        assert (repo / "sccache.conf").read_text() == PACKAGE_SCCACHE_CONF.read_text()
        contents = env_file.read_text()
        assert "SCCACHE=true" in contents
        assert "SCCACHE_CONF=/ceph/sccache.conf" in contents
        assert "SCCACHE_DIR=/sccache" in contents
        assert "SCCACHE_CACHE_SIZE=100G" in contents
        assert "SCCACHE_S3_NO_CREDENTIALS" not in contents
        assert "SCCACHE_LOG=" not in contents
        expected_cache = (data_dir.parent / "cache" / "sccache").resolve()
        assert f"--volume={expected_cache}:{CONTAINER_SCCACHE_DIR}:Z" in extra_args
        assert expected_cache.is_dir()

    def test_prepare_build_env_enables_sccache_debug_when_configured(self, tmp_path):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(repo)
        config["containers"]["ceph_node"]["sccache_debug"] = True
        env_file, _extra_args = CephNode()._prepare_build_env()
        assert "SCCACHE_LOG=debug" in env_file.read_text()

    def test_prepare_build_env_uses_s3_sccache_when_configured(self, tmp_path):
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(repo)
        config["containers"]["ceph_node"]["sccache_mode"] = "s3"
        env_file, extra_args = CephNode()._prepare_build_env()
        assert (
            repo / "sccache.conf"
        ).read_text() == PACKAGE_SCCACHE_S3_CONF.read_text()
        contents = env_file.read_text()
        assert "SCCACHE_S3_NO_CREDENTIALS=true" in contents
        assert "SCCACHE_S3_RW_MODE=READ_ONLY" in contents
        assert "SCCACHE_DIR=" not in contents
        assert extra_args == []

    def test_prepare_build_env_honors_custom_sccache_conf(self, tmp_path):
        custom_conf = tmp_path / "custom-sccache.conf"
        custom_conf.write_text("[cache.s3]\nbucket = test\n")
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(repo)
        config["containers"]["ceph_node"]["sccache_conf"] = str(custom_conf)
        config["containers"]["ceph_node"]["sccache_mode"] = "s3"
        env_file, extra_args = CephNode()._prepare_build_env()
        assert (repo / "sccache.conf").read_text() == custom_conf.read_text()
        contents = env_file.read_text()
        assert "SCCACHE_S3_NO_CREDENTIALS=true" in contents
        assert extra_args == []

    def test_prepare_build_env_skips_when_nothing_to_configure(self, tmp_path):
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(repo)
        config["containers"]["ceph_node"]["sccache"] = False
        assert CephNode()._prepare_build_env() == (None, [])

    def test_git_worktree_info_detects_linked_worktree(self, tmp_path):
        main_repo = tmp_path / "ceph"
        worktree = tmp_path / "ceph_main"
        admin_dir = main_repo / ".git" / "worktrees" / "ceph_main"
        admin_dir.mkdir(parents=True)
        (admin_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {admin_dir}\n",
            encoding="utf-8",
        )
        assert git_worktree_info(worktree) == (main_repo / ".git", "ceph_main")

    def test_prepare_build_env_mounts_git_metadata_for_worktree(self, tmp_path):
        main_repo = tmp_path / "ceph"
        worktree = tmp_path / "ceph_main"
        admin_dir = main_repo / ".git" / "worktrees" / "ceph_main"
        admin_dir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {admin_dir}\n",
            encoding="utf-8",
        )
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(worktree)
        config["containers"]["ceph_node"]["sccache"] = False
        env_file, extra_args = CephNode()._prepare_build_env()
        assert env_file is None
        expected_mounts = worktree_container_mounts(
            worktree, main_repo / ".git", "ceph_main"
        )
        assert extra_args == expected_mounts
        dot_git = worktree / REPO_DEVSTACK_DIR / "git" / "dot-git"
        assert dot_git.read_text() == (
            f"gitdir: {CONTAINER_GIT_METADATA_DIR}/worktrees/ceph_main\n"
        )

    def test_worktree_container_mounts_do_not_set_git_env(self, tmp_path):
        main_repo = tmp_path / "ceph"
        worktree = tmp_path / "ceph_main"
        admin_dir = main_repo / ".git" / "worktrees" / "ceph_main"
        admin_dir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {admin_dir}\n",
            encoding="utf-8",
        )
        config["data_dir"] = str(tmp_path / "data")
        config["containers"]["ceph_node"]["image"] = "example:test"
        config["containers"]["ceph_node"]["repo"] = str(worktree)
        config["containers"]["ceph_node"]["sccache"] = True
        env_file, extra_args = CephNode()._prepare_build_env()
        assert env_file is not None
        contents = env_file.read_text()
        assert "GIT_DIR=" not in contents
        assert "GIT_WORK_TREE=" not in contents
        assert len(extra_args) == 4

    def test_compile_cmd_passes_worktree_mount_and_env(self, tmp_path):
        main_repo = tmp_path / "ceph"
        worktree = tmp_path / "ceph_main"
        admin_dir = main_repo / ".git" / "worktrees" / "ceph_main"
        admin_dir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {admin_dir}\n",
            encoding="utf-8",
        )
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["repo"] = str(worktree)
        config["containers"]["ceph_node"]["build_dir"] = "build"
        config["containers"]["ceph_node"]["build_distro"] = "centos9"
        config["containers"]["ceph_node"]["sccache"] = False
        config["containers"]["ceph_node"]["npm_cache"] = False
        env_file = worktree / REPO_DEVSTACK_DIR / BUILD_ENV_NAME
        extra_args = worktree_container_mounts(
            worktree, main_repo / ".git", "ceph_main"
        )
        cmd = CephNode()._compile_cmd(env_file=env_file, extra_args=extra_args)
        assert cmd[cmd.index("--homedir") + 1] == "/ceph"
        assert "--env-file" in cmd
        assert str(env_file) in cmd
        for extra in extra_args:
            assert f"--extra={extra}" in cmd

    def test_bundled_sccache_conf_uses_local_disk(self):
        contents = PACKAGE_SCCACHE_CONF.read_text()
        assert "[cache.disk]" in contents
        assert 'dir = "/sccache"' in contents
        assert "rw_mode" not in contents

    def test_bundled_sccache_s3_conf_keeps_anonymous_read_settings(self):
        contents = PACKAGE_SCCACHE_S3_CONF.read_text()
        assert "[cache.s3]" in contents
        assert "no_credentials = true" in contents
        assert "rw_mode" not in contents

    def test_cpatch_cmd_uses_upstream_script(self):
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["base_image"] = "quay.io/ceph-ci/ceph:main"
        cmd = CephNode()._cpatch_cmd()
        assert cmd[0:2] == ["sudo", "../src/script/cpatch"]
        assert "localhost/ceph-devstack:main" in cmd

    async def test_build_runs_compile_then_cpatch(self, tmp_path):
        build_path = tmp_path / "build"
        build_path.mkdir()
        (build_path / "build.ninja").write_text("")
        config["containers"]["ceph_node"]["image"] = "localhost/ceph-devstack:main"
        config["containers"]["ceph_node"]["repo"] = str(tmp_path)
        config["containers"]["ceph_node"]["build_dir"] = "build"
        node = CephNode()
        with (
            patch.object(node, "_compile", new=AsyncMock()) as mock_compile,
            patch.object(node, "_build_image_cpatch", new=AsyncMock()) as mock_cpatch,
        ):
            await node.build()
            mock_compile.assert_awaited_once()
            mock_cpatch.assert_awaited_once()


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
