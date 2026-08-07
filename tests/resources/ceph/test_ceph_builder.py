"""Tests for CephBuilder resource."""

from pathlib import Path
from unittest.mock import AsyncMock, patch
import sys

import pytest
import tomlkit

from ceph_devstack import DEFAULT_CEPH_IMAGE, config
from ceph_devstack.resources.ceph.ceph_builder import (
    BUILD_ENV_NAME,
    CONTAINER_GIT_METADATA_DIR,
    CONTAINER_SCCACHE_DIR,
    DISTRO_PYTHON_VERSIONS,
    CephBuilder,
    PACKAGE_SCCACHE_CONF,
    PACKAGE_SCCACHE_S3_CONF,
    PYTHON_VERSION_DISTROS,
    REPO_DEVSTACK_DIR,
)


class TestCephBuilder:
    """Tests for CephBuilder compilation and build management."""

    def test_image_builder_uses_shipped_config_default(self):
        # Leave shipped containers.ceph_builder.image_builder in place.
        assert "image_builder" in config["containers"]["ceph_builder"]
        assert CephBuilder().image_builder == "binary-patch"

    def test_compile_steps_default_for_binary_patch(self):
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["image_builder"] = "binary-patch"
        assert CephBuilder().compile_steps == ["build"]

    def test_compile_steps_default_for_package_build(self):
        config["containers"]["ceph_builder"] = {}
        config["containers"]["ceph_builder"]["image_builder"] = "package-build"
        assert CephBuilder().compile_steps == ["packages"]

    def test_compile_cmd_uses_build_with_container(self, builder_config):
        builder_config(build_dir="build", build_distro="centos9")
        cmd = CephBuilder()._compile_cmd()
        assert cmd[0] in ["python3", sys.executable]
        assert "build-with-container.py" in cmd[1]
        assert "-d" in cmd and "centos9" in cmd
        assert "-b" in cmd and "build" in cmd
        assert cmd[cmd.index("--homedir") + 1] == "/ceph"
        assert "--env-file" not in cmd
        assert "--npm-cache-path" not in cmd

    def test_compile_cmd_passes_npm_cache_path(self, tmp_path, builder_config):
        npm_cache = tmp_path / "npm-cache"
        config["data_dir"] = str(tmp_path / "data")
        builder_config(
            repo=str(tmp_path / "ceph"), npm_cache=True, npm_cache_path=str(npm_cache)
        )
        cmd = CephBuilder()._compile_cmd()
        assert "--npm-cache-path" in cmd
        assert str(npm_cache.resolve()) in cmd
        assert npm_cache.is_dir()

    def test_compile_cmd_uses_default_npm_cache_under_data_dir(
        self, tmp_path, builder_config
    ):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        builder_config(repo=str(tmp_path / "ceph"), npm_cache=True)
        cmd = CephBuilder()._compile_cmd()
        expected = (data_dir / "cache" / "npm").resolve()
        assert "--npm-cache-path" in cmd
        assert str(expected) in cmd
        assert expected.is_dir()

    def test_compile_cmd_skips_ccache_dir(self, tmp_path, builder_config):
        builder_config(repo=str(tmp_path / "ceph"))
        cmd = CephBuilder()._compile_cmd()
        assert "--ccache-dir" not in cmd

    def test_compile_cmd_passes_dnf_cache_path_when_enabled(
        self, tmp_path, builder_config
    ):
        dnf_cache = tmp_path / "dnf-cache"
        builder_config(
            repo=str(tmp_path / "ceph"), dnf_cache=True, dnf_cache_path=str(dnf_cache)
        )
        cmd = CephBuilder()._compile_cmd()
        assert "--dnf-cache-path" in cmd
        assert str(dnf_cache.resolve()) in cmd
        assert dnf_cache.is_dir()

    def test_compile_cmd_passes_ceph_version_for_package_build(self, builder_config):
        builder_config(image_builder="package-build")
        cmd = CephBuilder()._compile_cmd(ceph_version="19.2.0-123-gabcd1234")
        assert "--ceph-version" in cmd
        assert cmd[cmd.index("--ceph-version") + 1] == "19.2.0-123-gabcd1234"

    def test_compile_cmd_requires_ceph_version_for_package_build(self, builder_config):
        builder_config(image_builder="package-build")
        with pytest.raises(ValueError, match="ceph_version is required"):
            CephBuilder()._compile_cmd()

    async def test_git_value_uses_cmd(self, tmp_path, builder_config):
        builder_config()
        mock_proc = AsyncMock()
        mock_proc.collect_output = AsyncMock(return_value=("v19.2.0-1-gabcd1234\n", ""))
        mock_proc.wait = AsyncMock(return_value=0)
        builder = CephBuilder()
        with patch.object(
            builder, "cmd", new=AsyncMock(return_value=mock_proc)
        ) as m_cmd:
            result = await builder._git_value("describe --abbrev=8 --match v*")
        assert result == "v19.2.0-1-gabcd1234"
        m_cmd.assert_awaited_once_with(
            ["git", "describe", "--abbrev=8", "--match", "v*"],
            check=True,
            cwd=Path(tmp_path),
        )

    async def test_compile_passes_repo_as_cwd(self, tmp_path, builder_config):
        builder_config()

        builder = CephBuilder()
        builder.cmd = AsyncMock()
        builder._prepare_build_env = lambda: (None, [])
        builder._build_cpatch_image = AsyncMock()
        builder._resolve_build_distro = AsyncMock()

        await builder.compile()

        assert Path(builder.cmd.await_args.kwargs["cwd"]) == tmp_path

    async def test_compile_passes_async_dist_version(self, tmp_path, builder_config):
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), image_builder="package-build")

        builder = CephBuilder()
        builder._make_dist = AsyncMock()
        builder._make_dist_version = AsyncMock(return_value="19.2.0-1-gabcd1234")
        builder.cmd = AsyncMock()
        builder._build_runtime_image = AsyncMock()
        builder._prepare_build_env = lambda: (None, [])

        await builder.compile()

        builder._make_dist_version.assert_awaited_once()
        builder._make_dist.assert_awaited_once_with(version="19.2.0-1-gabcd1234")
        builder._build_runtime_image.assert_awaited_once_with(
            ceph_version="19.2.0-1-gabcd1234"
        )
        cmd = builder.cmd.call_args[0][0]
        assert "--ceph-version" in cmd
        assert cmd[cmd.index("--ceph-version") + 1] == "19.2.0-1-gabcd1234"

    def test_prepare_build_env_uses_local_sccache_by_default(
        self, tmp_path, builder_config
    ):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), sccache=True)
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

    def test_prepare_build_env_enables_sccache_debug_when_configured(
        self, tmp_path, builder_config
    ):
        data_dir = tmp_path / "data"
        config["data_dir"] = str(data_dir)
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), sccache=True, sccache_debug=True)
        env_file, _extra_args = CephBuilder()._prepare_build_env()
        assert "SCCACHE_LOG=debug" in env_file.read_text()

    def test_prepare_build_env_uses_s3_sccache_when_configured(
        self, tmp_path, builder_config
    ):
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), sccache=True, sccache_mode="s3")
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
        self, tmp_path, monkeypatch, builder_config
    ):
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key-id")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(
            repo=str(repo), sccache=True, sccache_mode="s3", sccache_rw_mode=True
        )
        env_file, extra_args = CephBuilder()._prepare_build_env()
        contents = env_file.read_text()
        assert "AWS_ACCESS_KEY_ID=test-key-id" in contents
        assert "AWS_SECRET_ACCESS_KEY=test-secret-key" in contents
        assert "SCCACHE_S3_RW_MODE=READ_WRITE" in contents
        assert "SCCACHE_S3_NO_CREDENTIALS=true" not in contents
        assert extra_args == []

    def test_prepare_build_env_raises_error_for_s3_rw_without_credentials(
        self, tmp_path, monkeypatch, builder_config
    ):
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(
            repo=str(repo), sccache=True, sccache_mode="s3", sccache_rw_mode=True
        )
        with pytest.raises(
            ValueError, match="AWS_ACCESS_KEY_ID.*AWS_SECRET_ACCESS_KEY"
        ):
            CephBuilder()._prepare_build_env()

    def test_prepare_build_env_honors_custom_sccache_conf(
        self, tmp_path, builder_config
    ):
        custom_conf = tmp_path / "custom-sccache.conf"
        custom_conf.write_text('[cache.s3]\nbucket = "test"\n')
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(
            repo=str(repo),
            sccache=True,
            sccache_conf=str(custom_conf),
            sccache_mode="s3",
        )
        env_file, extra_args = CephBuilder()._prepare_build_env()
        sccache_conf = repo / "sccache.conf"
        conf_data = tomlkit.parse(sccache_conf.read_text())
        assert conf_data["cache"]["s3"]["bucket"] == "test"
        assert conf_data["cache"]["s3"]["no_credentials"] is True
        contents = env_file.read_text()
        assert "SCCACHE_S3_NO_CREDENTIALS=true" in contents
        assert extra_args == []

    def test_prepare_build_env_without_sccache_still_writes_core_env(
        self, tmp_path, builder_config
    ):
        """Even with sccache off, cmake/telemetry/dwz env vars are always written."""
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo))
        env_file, extra_args = CephBuilder()._prepare_build_env()
        assert env_file == repo / REPO_DEVSTACK_DIR / BUILD_ENV_NAME
        assert extra_args == []
        contents = env_file.read_text()
        assert "CEPH_EXTRA_CMAKE_ARGS=" in contents
        assert "-DWITH_SCCACHE=ON" not in contents
        assert "IBM_TELEMETRY_DISABLED=true" in contents
        assert "DWZ=false" in contents
        assert "SCCACHE=" not in contents

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
        assert CephBuilder.git_worktree_info(worktree) == (
            main_repo / ".git",
            "ceph_main",
        )

    def test_prepare_build_env_mounts_git_metadata_for_worktree(
        self, tmp_path, builder_config
    ):
        main_repo = tmp_path / "ceph"
        worktree = tmp_path / "ceph_main"
        admin_dir = main_repo / ".git" / "worktrees" / "ceph_main"
        admin_dir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {admin_dir}\n",
            encoding="utf-8",
        )
        builder_config(repo=str(worktree))
        _env_file, extra_args = CephBuilder()._prepare_build_env()
        dot_git = worktree / REPO_DEVSTACK_DIR / "git" / "dot-git"
        assert dot_git.exists()
        assert f"--volume={dot_git}:/ceph/.git:Z,ro" in extra_args
        assert (
            f"--volume={main_repo / '.git'}:{CONTAINER_GIT_METADATA_DIR}:Z,ro"
            in extra_args
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
        mounts = CephBuilder.worktree_container_mounts(
            worktree, main_repo / ".git", "ceph_main"
        )
        for mount in mounts:
            assert not mount.startswith("-e")

    def test_compile_cmd_passes_worktree_mount_and_env(self, tmp_path, builder_config):
        main_repo = tmp_path / "ceph"
        worktree = tmp_path / "ceph_main"
        admin_dir = main_repo / ".git" / "worktrees" / "ceph_main"
        admin_dir.mkdir(parents=True)
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {admin_dir}\n",
            encoding="utf-8",
        )
        builder_config(repo=str(worktree))
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

    async def test_build_creates_builder_image(self, tmp_path, builder_config):
        """Test that build() calls build-with-container.py to build builder image."""
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), build_image=True)

        builder = CephBuilder()
        builder.cmd = AsyncMock()
        builder._resolve_build_distro = AsyncMock()

        await builder.build()

        builder.cmd.assert_awaited_once()
        cmd = builder.cmd.call_args[0][0]
        assert cmd[0] in ["python3", sys.executable]
        assert "build-with-container.py" in cmd[1]
        assert "-d" in cmd and "centos9" in cmd
        assert "-e" in cmd
        assert cmd[cmd.index("-e") + 1] == "build-container"

    @pytest.mark.asyncio
    async def test_pull_uses_minimal_command(self, builder_config):
        """Test that pull() uses a minimal command with --image-sources pull."""
        builder_config(build_distro="centos9")

        builder = CephBuilder()
        builder.cmd = AsyncMock()
        builder._resolve_build_distro = AsyncMock()

        await builder.pull()

        builder.cmd.assert_called_once()
        cmd = builder.cmd.call_args[0][0]

        # Should have minimal args: python, script, distro, image-sources, execute container
        assert cmd[0] in ["python3", sys.executable]
        assert "build-with-container.py" in cmd[1]
        assert "-d" in cmd and "centos9" in cmd
        assert "--image-sources" in cmd
        assert cmd[cmd.index("--image-sources") + 1] == "pull"
        assert "-e" in cmd
        assert cmd[cmd.index("-e") + 1] == "container"

        # Should NOT have compilation-related args
        assert "-b" not in cmd  # no build dir
        assert "--homedir" not in cmd  # no homedir

    @pytest.mark.asyncio
    async def test_pull_works_for_package_build_mode(self, builder_config):
        """Test that pull() works in package-build mode."""
        builder_config(image_builder="package-build", build_distro="centos9")

        builder = CephBuilder()
        builder.cmd = AsyncMock()

        await builder.pull()

        builder.cmd.assert_awaited_once()
        cmd = builder.cmd.call_args[0][0]
        assert "build-with-container.py" in cmd[1]

    @pytest.mark.asyncio
    async def test_pull_skips_when_no_repo(self, builder_config):
        """Test that pull() skips when no repo is configured."""
        builder_config(repo="")

        builder = CephBuilder()
        builder.cmd = AsyncMock()

        await builder.pull()

        builder.cmd.assert_not_called()

    async def test_build_cpatch_image_invokes_cpatch_and_tags(
        self, tmp_path, builder_config
    ):
        repo = tmp_path / "ceph"
        build_dir = repo / "build"
        build_dir.mkdir(parents=True)
        cpatch = repo / "src" / "script" / "cpatch.py"
        cpatch.parent.mkdir(parents=True)
        cpatch.write_text("#!/usr/bin/env python3\n")
        builder_config(repo=str(repo), target_image=DEFAULT_CEPH_IMAGE)
        builder = CephBuilder()
        builder.cmd = AsyncMock()

        await builder._build_cpatch_image()

        assert builder.cmd.await_count == 2

        cpatch_cmd = builder.cmd.call_args_list[0][0][0]
        assert cpatch_cmd[0] in ["python3", sys.executable]
        assert cpatch_cmd[1] == str(cpatch)
        assert "--base" in cpatch_cmd
        assert DEFAULT_CEPH_IMAGE in cpatch_cmd
        assert "--target" in cpatch_cmd
        assert builder.runtime_image_name in cpatch_cmd
        assert "-b" in cpatch_cmd
        assert str(build_dir) in cpatch_cmd
        assert builder.cmd.call_args_list[0].kwargs["cwd"] == repo

        tag_cmd = builder.cmd.call_args_list[1][0][0]
        assert tag_cmd[0] == "podman"
        assert tag_cmd[1] == "tag"
        assert tag_cmd[2] == builder.runtime_image_name
        assert tag_cmd[3].startswith("localhost/ceph-cpatch:")
        assert tag_cmd[3].endswith("-devel")

    async def test_build_cpatch_image_raises_when_cpatch_missing(
        self, tmp_path, builder_config
    ):
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo))
        builder = CephBuilder()
        builder.cmd = AsyncMock()

        with pytest.raises(FileNotFoundError, match="cpatch script not found"):
            await builder._build_cpatch_image()

    def test_should_build_defaults_to_false(self, builder_config):
        builder_config()
        assert CephBuilder().should_build is False

    def test_should_build_true_when_build_image_set(self, builder_config):
        builder_config(build_image=True)
        assert CephBuilder().should_build is True

    def test_compile_cmd_includes_image_sources_cache_pull_by_default(
        self, builder_config
    ):
        builder_config()
        cmd = CephBuilder()._compile_cmd()
        assert "--image-sources" in cmd
        assert cmd[cmd.index("--image-sources") + 1] == "cache,pull"

    def test_compile_cmd_omits_image_sources_when_build_image_true(
        self, builder_config
    ):
        builder_config(build_image=True)
        cmd = CephBuilder()._compile_cmd()
        assert "--image-sources" not in cmd

    async def test_build_skips_when_build_image_not_set(self, builder_config):
        builder_config()
        builder = CephBuilder()
        builder.cmd = AsyncMock()
        await builder.build()
        builder.cmd.assert_not_called()

    async def test_build_runs_when_build_image_true(self, builder_config):
        builder_config(build_image=True)
        builder = CephBuilder()
        builder.cmd = AsyncMock()
        builder._resolve_build_distro = AsyncMock()
        await builder.build()
        builder.cmd.assert_awaited_once()

    def test_bwc_base_cmd_includes_image_repo_when_configured(self, builder_config):
        builder_config(builder_image_repo="quay.ceph.io/ceph-ci/ceph-build")
        builder = CephBuilder()
        cmd = builder._bwc_base_cmd()
        assert "--image-repo" in cmd
        assert cmd[cmd.index("--image-repo") + 1] == "quay.ceph.io/ceph-ci/ceph-build"

    def test_bwc_base_cmd_omits_image_repo_when_not_configured(self, builder_config):
        builder_config()
        builder = CephBuilder()
        cmd = builder._bwc_base_cmd()
        assert "--image-repo" not in cmd

    @patch("ceph_devstack.resources.ceph.ceph_builder.host_arch", return_value="x86_64")
    def test_bwc_base_cmd_includes_tag_suffix_when_not_building(
        self, _mock, builder_config
    ):
        builder_config()
        builder = CephBuilder()
        cmd = builder._bwc_base_cmd()
        assert "--tag" in cmd
        assert cmd[cmd.index("--tag") + 1] == "+x86_64.default"

    def test_bwc_base_cmd_omits_tag_when_building(self, builder_config):
        builder_config(build_image=True)
        builder = CephBuilder()
        cmd = builder._bwc_base_cmd()
        assert "--tag" not in cmd

    async def test_detect_target_python_parses_version(self, builder_config):
        builder_config(target_image="quay.ceph.io/ceph-ci/ceph:main")
        builder = CephBuilder()
        mock_proc = AsyncMock()
        mock_proc.collect_output = AsyncMock(return_value=("3.12\n", ""))
        builder.cmd = AsyncMock(return_value=mock_proc)

        result = await builder._detect_target_python()

        assert result == "3.12"
        cmd = builder.cmd.call_args[0][0]
        assert cmd[:2] == ["podman", "run"]
        assert "--entrypoint" in cmd
        assert "python3" in cmd

    async def test_detect_target_python_returns_none_on_failure(self, builder_config):
        from subprocess import CalledProcessError

        builder_config(target_image="quay.ceph.io/ceph-ci/ceph:main")
        builder = CephBuilder()
        builder.cmd = AsyncMock(side_effect=CalledProcessError(1, "podman"))

        result = await builder._detect_target_python()

        assert result is None

    async def test_resolve_build_distro_auto_selects_when_not_configured(
        self, builder_config
    ):
        builder_config(target_image="quay.ceph.io/ceph-ci/ceph:main")
        builder = CephBuilder()
        mock_proc = AsyncMock()
        mock_proc.collect_output = AsyncMock(return_value=("3.12\n", ""))
        builder.cmd = AsyncMock(return_value=mock_proc)

        await builder._resolve_build_distro()

        assert builder.distro == "centos10"
        assert "build_distro" not in config["containers"]["ceph_builder"]

    async def test_resolve_build_distro_respects_explicit_config(self, builder_config):
        builder_config(
            target_image="quay.ceph.io/ceph-ci/ceph:main", build_distro="centos9"
        )
        builder = CephBuilder()
        mock_proc = AsyncMock()
        mock_proc.collect_output = AsyncMock(return_value=("3.12\n", ""))
        builder.cmd = AsyncMock(return_value=mock_proc)

        await builder._resolve_build_distro()

        assert builder.distro == "centos9"

    async def test_resolve_build_distro_keeps_default_when_python_matches(
        self, builder_config
    ):
        builder_config(target_image="quay.ceph.io/ceph-ci/ceph:main")
        builder = CephBuilder()
        mock_proc = AsyncMock()
        mock_proc.collect_output = AsyncMock(return_value=("3.9\n", ""))
        builder.cmd = AsyncMock(return_value=mock_proc)

        await builder._resolve_build_distro()

        assert builder.distro == "centos9"

    def test_distro_python_version_mapping_consistency(self):
        for py_ver, distro in PYTHON_VERSION_DISTROS.items():
            assert DISTRO_PYTHON_VERSIONS[distro] == py_ver
