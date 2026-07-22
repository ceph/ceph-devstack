"""Tests for CephBuilder resource."""
from unittest.mock import AsyncMock, patch
import sys

import pytest
import tomlkit

from ceph_devstack import config
from ceph_devstack.resources.ceph.ceph_builder import (
    BUILD_ENV_NAME,
    CONTAINER_GIT_METADATA_DIR,
    CONTAINER_SCCACHE_DIR,
    CephBuilder,
    PACKAGE_SCCACHE_CONF,
    PACKAGE_SCCACHE_S3_CONF,
    REPO_DEVSTACK_DIR,
    git_worktree_info,
    worktree_container_mounts,
)


class TestCephBuilder:
    """Tests for CephBuilder compilation and build management."""

    def test_compile_steps_default_for_binary_patch(self):
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["image_builder"] = "binary-patch"
        assert CephBuilder().compile_steps == ["build"]

    def test_compile_steps_default_for_package_build(self):
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["image_builder"] = "package-build"
        assert CephBuilder().compile_steps == ["packages"]

    def test_compile_cmd_uses_build_with_container(self, tmp_path):
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path)
        config["containers"]["ceph_builder"]["build_dir"] = "build"
        config["containers"]["ceph_builder"]["build_distro"] = "centos9"
        config["containers"]["ceph_builder"]["sccache"] = False
        config["containers"]["ceph_builder"]["npm_cache"] = False
        cmd = CephBuilder()._compile_cmd()
        assert cmd[0] in ["python3", sys.executable]
        assert "build-with-container.py" in cmd[1]
        assert "-d" in cmd and "centos9" in cmd
        assert "-b" in cmd and "build" in cmd
        assert cmd[cmd.index("--homedir") + 1] == "/ceph"
        assert "--env-file" not in cmd
        assert "--npm-cache-path" not in cmd

    def test_compile_cmd_passes_npm_cache_path(self, tmp_path):
        npm_cache = tmp_path / "npm-cache"
        config["data_dir"] = str(tmp_path / "data")
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_builder"]["npm_cache_path"] = str(npm_cache)
        config["containers"]["ceph_builder"]["sccache"] = False
        cmd = CephBuilder()._compile_cmd()
        assert "--npm-cache-path" in cmd
        assert str(npm_cache.resolve()) in cmd
        assert npm_cache.is_dir()

    def test_compile_cmd_uses_default_npm_cache_under_data_dir(self, tmp_path):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_builder"]["sccache"] = False
        cmd = CephBuilder()._compile_cmd()
        expected = (data_dir / "cache" / "npm").resolve()
        assert "--npm-cache-path" in cmd
        assert str(expected) in cmd
        assert expected.is_dir()

    def test_compile_cmd_skips_ccache_dir(self, tmp_path):
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_builder"]["npm_cache"] = False
        cmd = CephBuilder()._compile_cmd()
        assert "--ccache-dir" not in cmd

    def test_compile_cmd_passes_dnf_cache_path_when_enabled(self, tmp_path):
        dnf_cache = tmp_path / "dnf-cache"
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_builder"]["npm_cache"] = False
        config["containers"]["ceph_builder"]["sccache"] = False
        config["containers"]["ceph_builder"]["dnf_cache"] = True
        config["containers"]["ceph_builder"]["dnf_cache_path"] = str(dnf_cache)
        cmd = CephBuilder()._compile_cmd()
        assert "--dnf-cache-path" in cmd
        assert str(dnf_cache.resolve()) in cmd
        assert dnf_cache.is_dir()

    def test_prepare_build_env_uses_local_sccache_by_default(self, tmp_path):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        env_file, extra_args = CephBuilder()._prepare_build_env()
        assert env_file == repo / REPO_DEVSTACK_DIR / BUILD_ENV_NAME
        assert (repo / "sccache.conf").read_text() == PACKAGE_SCCACHE_CONF.read_text()
        contents = env_file.read_text()
        assert "SCCACHE=true" in contents
        assert "SCCACHE_CONF=/ceph/sccache.conf" in contents
        assert "SCCACHE_DIR=/sccache" in contents
        assert "SCCACHE_CACHE_SIZE=100G" in contents
        assert "SCCACHE_S3_NO_CREDENTIALS" not in contents
        assert "SCCACHE_LOG=" not in contents
        expected_cache = (data_dir / "cache" / "sccache").resolve()
        assert f"--volume={expected_cache}:{CONTAINER_SCCACHE_DIR}:Z" in extra_args
        assert expected_cache.is_dir()

    def test_prepare_build_env_enables_sccache_debug_when_configured(self, tmp_path):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        config["containers"]["ceph_builder"]["sccache_debug"] = True
        env_file, _extra_args = CephBuilder()._prepare_build_env()
        assert "SCCACHE_LOG=debug" in env_file.read_text()

    def test_prepare_build_env_uses_s3_sccache_when_configured(self, tmp_path):
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        config["containers"]["ceph_builder"]["sccache_mode"] = "s3"
        env_file, extra_args = CephBuilder()._prepare_build_env()
        sccache_conf = repo / "sccache.conf"
        assert sccache_conf.exists()
        conf_data = tomlkit.parse(sccache_conf.read_text())
        assert conf_data["cache"]["s3"]["no_credentials"] is True
        contents = env_file.read_text()
        assert "SCCACHE_S3_NO_CREDENTIALS=true" in contents
        assert "SCCACHE_S3_RW_MODE=READ_ONLY" in contents
        assert "SCCACHE_DIR=" not in contents
        assert extra_args == []

    def test_prepare_build_env_uses_s3_rw_mode_with_credentials(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key-id")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        config["containers"]["ceph_builder"]["sccache_mode"] = "s3"
        config["containers"]["ceph_builder"]["sccache_rw_mode"] = True
        env_file, extra_args = CephBuilder()._prepare_build_env()
        contents = env_file.read_text()
        assert "AWS_ACCESS_KEY_ID=test-key-id" in contents
        assert "AWS_SECRET_ACCESS_KEY=test-secret-key" in contents
        assert "SCCACHE_S3_RW_MODE=READ_WRITE" in contents
        assert "SCCACHE_S3_NO_CREDENTIALS=true" not in contents
        assert extra_args == []

    def test_prepare_build_env_raises_error_for_s3_rw_without_credentials(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        config["containers"]["ceph_builder"]["sccache_mode"] = "s3"
        config["containers"]["ceph_builder"]["sccache_rw_mode"] = True
        with pytest.raises(
            ValueError, match="AWS_ACCESS_KEY_ID.*AWS_SECRET_ACCESS_KEY"
        ):
            CephBuilder()._prepare_build_env()

    def test_prepare_build_env_honors_custom_sccache_conf(self, tmp_path):
        custom_conf = tmp_path / "custom-sccache.conf"
        custom_conf.write_text('[cache.s3]\nbucket = "test"\n')
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        config["containers"]["ceph_builder"]["sccache_conf"] = str(custom_conf)
        config["containers"]["ceph_builder"]["sccache_mode"] = "s3"
        env_file, extra_args = CephBuilder()._prepare_build_env()
        sccache_conf = repo / "sccache.conf"
        conf_data = tomlkit.parse(sccache_conf.read_text())
        assert conf_data["cache"]["s3"]["bucket"] == "test"
        assert conf_data["cache"]["s3"]["no_credentials"] is True
        contents = env_file.read_text()
        assert "SCCACHE_S3_NO_CREDENTIALS=true" in contents
        assert extra_args == []

    def test_prepare_build_env_skips_when_nothing_to_configure(self, tmp_path):
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        config["containers"]["ceph_builder"]["sccache"] = False
        assert CephBuilder()._prepare_build_env() == (None, [])

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
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(worktree)
        config["containers"]["ceph_builder"]["sccache"] = False
        _env_file, extra_args = CephBuilder()._prepare_build_env()
        dot_git = worktree / REPO_DEVSTACK_DIR / "git" / "dot-git"
        assert dot_git.exists()
        assert f"--volume={dot_git}:/ceph/.git:Z,ro" in extra_args
        assert f"--volume={main_repo / '.git'}:{CONTAINER_GIT_METADATA_DIR}:Z,ro" in extra_args

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
        mounts = worktree_container_mounts(worktree, main_repo / ".git", "ceph_main")
        for mount in mounts:
            assert not mount.startswith("-e")

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
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(worktree)
        config["containers"]["ceph_builder"]["sccache"] = False
        env_file = worktree / REPO_DEVSTACK_DIR / BUILD_ENV_NAME
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("TEST=1\n")
        cmd = CephBuilder()._compile_cmd(env_file=env_file)
        assert "--env-file" in cmd
        assert str(env_file) in cmd

    def test_bundled_sccache_conf_uses_local_disk(self):
        contents = PACKAGE_SCCACHE_CONF.read_text()
        assert "[cache.disk]" in contents

    def test_bundled_sccache_s3_conf_has_s3_settings(self):
        contents = PACKAGE_SCCACHE_S3_CONF.read_text()
        assert "[cache.s3]" in contents

    async def test_build_creates_builder_image(self, tmp_path):
        """Test that build() calls build-with-container.py to build builder image."""
        repo = tmp_path / "ceph"
        repo.mkdir()
        
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["repo"] = str(repo)
        
        builder = CephBuilder()
        
        # Mock _run_cmd to avoid actually running build-with-container.py
        with patch.object(builder, '_run_cmd', new=AsyncMock()) as mock_run:
            await builder.build()
            
            # Should have called _run_cmd with build-with-container.py command
            mock_run.assert_awaited_once()
            call_args = mock_run.call_args[0]
            assert "build-with-container.py" in str(call_args[0])