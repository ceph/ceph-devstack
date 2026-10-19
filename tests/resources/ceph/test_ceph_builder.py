"""Tests for CephBuilder resource."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import sys

import pytest
import tomlkit

from ceph_devstack import DEFAULT_CEPH_IMAGE, config
from ceph_devstack.resources.utils import host_arch
from ceph_devstack.resources.ceph.ceph_builder import (
    BUILD_ENV_NAME,
    CONTAINER_GIT_METADATA_DIR,
    CONTAINER_SCCACHE_DIR,
    CephBuilder,
    ImageBuilder,
    PACKAGE_SCCACHE_CONF,
    PACKAGE_SCCACHE_S3_CONF,
    REPO_DEVSTACK_DIR,
)


class TestCephBuilder:
    """Tests for CephBuilder compilation and build management."""

    def test_image_builder_uses_shipped_config_default(self):
        # Leave shipped containers.ceph_builder.image_builder in place.
        assert "image_builder" in config["containers"]["ceph_builder"]
        assert CephBuilder().image_builder is ImageBuilder.BINARY_PATCH

    def test_image_builder_rejects_unknown_value(self, builder_config):
        builder_config(image_builder="bogus")
        with pytest.raises(ValueError, match="image_builder: expected one of"):
            _ = CephBuilder().image_builder

    @pytest.mark.parametrize(
        "image_builder, steps",
        [("binary-patch", ["build"]), ("package-build", ["packages"])],
    )
    def test_compile_steps_default(self, builder_config, image_builder, steps):
        builder_config(image_builder=image_builder)
        assert CephBuilder().compile_steps == steps

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
        builder_config(
            repo=str(tmp_path / "ceph"), npm_cache=True, npm_cache_path=str(npm_cache)
        )
        cmd = CephBuilder(data_dir=tmp_path / "data")._compile_cmd()
        assert "--npm-cache-path" in cmd
        assert str(npm_cache.resolve()) in cmd
        assert npm_cache.is_dir()

    def test_compile_cmd_uses_default_npm_cache_under_data_dir(
        self, tmp_path, builder_config
    ):
        data_dir = tmp_path / "data"
        builder_config(repo=str(tmp_path / "ceph"), npm_cache=True)
        cmd = CephBuilder(data_dir=data_dir)._compile_cmd()
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

    def test_make_dist_version_strips_v_from_git_describe(
        self, tmp_path, builder_config
    ):
        builder_config()
        with patch(
            "ceph_devstack.resources.ceph.ceph_builder.git_output",
            return_value="v19.2.0-1-gabcd1234",
        ) as m_git:
            assert CephBuilder()._make_dist_version() == "19.2.0-1-gabcd1234"
        m_git.assert_called_once_with(
            tmp_path.resolve(), "describe", "--abbrev=8", "--match", "v*"
        )

    def test_make_dist_version_raises_without_tag(self, builder_config):
        builder_config(image_builder="package-build")
        with (
            patch(
                "ceph_devstack.resources.ceph.ceph_builder.git_output",
                return_value=None,
            ),
            pytest.raises(
                RuntimeError, match="no v\\* tag, or not a usable git checkout"
            ),
        ):
            CephBuilder()._make_dist_version()

    async def test_compile_passes_repo_as_cwd(self, tmp_path, builder_config):
        builder_config()

        builder = CephBuilder()
        builder.cmd = AsyncMock()
        builder._prepare_build_env = lambda: (None, [])
        builder._build_cpatch_image = AsyncMock()
        builder.resolve_distro = AsyncMock()

        await builder.compile()

        assert Path(builder.cmd.await_args.kwargs["cwd"]) == tmp_path

    async def test_compile_passes_dist_version_for_package_build(
        self, tmp_path, builder_config
    ):
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), image_builder="package-build")

        builder = CephBuilder()
        builder._make_dist = AsyncMock()
        builder._make_dist_version = MagicMock(return_value="19.2.0-1-gabcd1234")
        builder.cmd = AsyncMock()
        builder._build_runtime_image = AsyncMock()
        builder._prepare_build_env = lambda: (None, [])

        await builder.compile()

        builder._make_dist_version.assert_called_once()
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
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), sccache=True)
        env_file, extra_args = CephBuilder(data_dir=data_dir)._prepare_build_env()
        assert env_file == repo / REPO_DEVSTACK_DIR / BUILD_ENV_NAME
        sccache_conf = repo / REPO_DEVSTACK_DIR / "sccache.conf"
        assert sccache_conf.read_text() == PACKAGE_SCCACHE_CONF.read_text()
        assert not (repo / "sccache.conf").exists()
        contents = env_file.read_text()
        assert "SCCACHE=true" in contents
        assert f"SCCACHE_CONF=/ceph/{REPO_DEVSTACK_DIR}/sccache.conf" in contents
        assert (
            f"SCCACHE_ERROR_LOG=/ceph/{REPO_DEVSTACK_DIR}/sccache_log.txt" in contents
        )
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
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), sccache=True, sccache_debug=True)
        env_file, _extra_args = CephBuilder(data_dir=data_dir)._prepare_build_env()
        assert "SCCACHE_LOG=debug" in env_file.read_text()

    def test_prepare_build_env_uses_s3_sccache_when_configured(
        self, tmp_path, builder_config
    ):
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), sccache=True, sccache_mode="s3")
        env_file, extra_args = CephBuilder()._prepare_build_env()
        sccache_conf = repo / REPO_DEVSTACK_DIR / "sccache.conf"
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
        sccache_conf = repo / REPO_DEVSTACK_DIR / "sccache.conf"
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
        builder.resolve_distro = AsyncMock()

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
        builder.resolve_distro = AsyncMock()

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

    async def test_build_cpatch_image_invokes_cpatch_and_tags(
        self, tmp_path, builder_config
    ):
        repo = tmp_path / "ceph"
        build_dir = repo / "build"
        build_dir.mkdir(parents=True)
        cpatch = repo / "src" / "script" / "cpatch.py"
        cpatch.parent.mkdir(parents=True)
        cpatch.write_text("#!/usr/bin/env python3\n")
        builder_config(repo=str(repo), base_image=DEFAULT_CEPH_IMAGE)
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
        assert tag_cmd[3].startswith("localhost/ceph-cpatch:main-centos-stream9-")
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

    @pytest.mark.parametrize("build_image", [False, True])
    def test_should_build_follows_build_image(self, builder_config, build_image):
        builder_config(build_image=build_image)
        assert CephBuilder().should_build is build_image

    @pytest.mark.parametrize("build_image", [False, True])
    def test_compile_cmd_image_sources(self, builder_config, build_image):
        builder_config(build_image=build_image)
        cmd = CephBuilder()._compile_cmd()
        if build_image:
            assert "--image-sources" not in cmd
        else:
            assert cmd[cmd.index("--image-sources") + 1] == "cache,pull"

    @pytest.mark.parametrize("build_image", [False, True])
    async def test_build_runs_only_with_build_image(self, builder_config, build_image):
        builder_config(build_image=build_image)
        builder = CephBuilder()
        builder.cmd = AsyncMock()
        builder.resolve_distro = AsyncMock()
        await builder.build()
        assert builder.cmd.await_count == (1 if build_image else 0)

    @pytest.mark.parametrize("image_repo", [None, "quay.ceph.io/ceph-ci/ceph-build"])
    def test_bwc_base_cmd_image_repo(self, builder_config, image_repo):
        if image_repo:
            builder_config(builder_image_repo=image_repo)
        else:
            builder_config()
        cmd = CephBuilder()._bwc_base_cmd()
        if image_repo:
            assert cmd[cmd.index("--image-repo") + 1] == image_repo
        else:
            assert "--image-repo" not in cmd

    @pytest.mark.parametrize("build_image", [False, True])
    @patch("ceph_devstack.resources.ceph.ceph_builder.host_arch", return_value="x86_64")
    def test_bwc_base_cmd_tag_suffix(self, _mock, builder_config, build_image):
        builder_config(build_image=build_image)
        cmd = CephBuilder()._bwc_base_cmd()
        if build_image:
            assert "--tag" not in cmd
        else:
            assert cmd[cmd.index("--tag") + 1] == "+x86_64.default"

    @pytest.mark.parametrize(
        "image_builder, base_image, expected",
        [
            ("binary-patch", DEFAULT_CEPH_IMAGE, "localhost/ceph-cpatch:main"),
            ("package-build", DEFAULT_CEPH_IMAGE, "localhost/ceph-devstack:main"),
            ("package-build", "localhost/ceph:custom", "localhost/ceph:custom"),
        ],
    )
    def test_runtime_image_name(
        self, builder_config, image_builder, base_image, expected
    ):
        builder_config(image_builder=image_builder, base_image=base_image)
        assert CephBuilder().runtime_image_name == expected

    @pytest.mark.parametrize("image_builder", ["binary-patch", "package-build"])
    def test_has_artifacts(self, tmp_path, builder_config, image_builder):
        builder_config(image_builder=image_builder)
        builder = CephBuilder()
        assert builder._has_artifacts() is False
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        if image_builder == "binary-patch":
            (build_dir / "build.ninja").touch()
        else:
            rpms = build_dir / "rpmbuild" / "RPMS" / "x86_64"
            rpms.mkdir(parents=True)
            (rpms / "ceph-base.rpm").touch()
        assert builder._has_artifacts() is True

    def test_build_subdir_resolves_absolute_path_inside_repo(
        self, tmp_path, builder_config
    ):
        builder_config(build_dir=str(tmp_path / "out" / "x"))
        assert CephBuilder().build_subdir == "out/x"

    def test_build_subdir_rejects_path_outside_repo(self, tmp_path, builder_config):
        repo = tmp_path / "ceph"
        repo.mkdir()
        builder_config(repo=str(repo), build_dir=str(tmp_path / "elsewhere"))
        with pytest.raises(ValueError, match="is not inside repo"):
            _ = CephBuilder().build_subdir

    def test_has_artifacts_package_build_ignores_stray_rpms(
        self, tmp_path, builder_config
    ):
        builder_config(image_builder="package-build")
        (tmp_path / "build" / "src").mkdir(parents=True)
        (tmp_path / "build" / "src" / "stray.rpm").touch()
        assert CephBuilder()._has_artifacts() is False

    async def test_start_raises_when_compile_leaves_no_artifacts(self, builder_config):
        builder_config()
        builder = CephBuilder()
        builder.compile = AsyncMock()
        builder._runtime_image_exists = AsyncMock(return_value=False)
        with pytest.raises(FileNotFoundError, match="no build.ninja"):
            await builder.start()
        builder.compile.assert_awaited_once()

    @pytest.mark.parametrize("action", ["pull", "build", "start"])
    async def test_actions_warn_and_skip_without_repo(
        self, builder_config, caplog, action
    ):
        builder_config(repo="", build_image=True)
        builder = CephBuilder()
        builder.cmd = AsyncMock()
        with caplog.at_level(logging.WARNING):
            await getattr(builder, action)()
        builder.cmd.assert_not_called()
        assert "no repo configured" in caplog.text

    async def test_compile_rejects_package_build_for_deb_distro(self, builder_config):
        builder_config(image_builder="package-build", build_distro="ubuntu24.04")
        builder = CephBuilder()
        builder.cmd = AsyncMock()
        with pytest.raises(ValueError, match="needs an RPM build_distro"):
            await builder.compile()
        builder.cmd.assert_not_called()

    def test_submodule_mounts_warn_when_git_fails(self, tmp_path, caplog):
        with (
            patch(
                "ceph_devstack.resources.ceph.ceph_builder.git_output",
                return_value=None,
            ),
            caplog.at_level(logging.WARNING),
        ):
            mounts = CephBuilder._worktree_submodule_git_mounts(
                tmp_path, "wt", tmp_path / "overlay"
            )
        assert mounts == []
        assert "could not list submodules" in caplog.text


class TestCephBuilderCiImageTags:
    def test_empty_without_repo(self, builder_config):
        builder_config(repo="")
        assert CephBuilder().ci_image_tags() == {}

    @patch("ceph_devstack.resources.ceph.ceph_builder.git_sha1", return_value="abc")
    @patch("ceph_devstack.resources.ceph.ceph_builder.git_branch", return_value="main")
    def test_uses_repo_head_and_distro(
        self, m_branch, _m_sha1, tmp_path, builder_config
    ):
        builder_config(build_distro="rocky10")
        assert CephBuilder().ci_image_tags() == {
            "full": f"main-rockylinux-10-{host_arch()}-devel",
            "branch": "main-rockylinux-10",
            "sha1": "abc-rockylinux-10",
        }
        m_branch.assert_called_once_with(tmp_path.resolve())


class TestCephBuilderRuntimeImage:
    async def test_compile_triggers_runtime_image_for_package_build(
        self, tmp_path, builder_config
    ):
        builder_config(image_builder="package-build", build_distro="centos9")
        builder = CephBuilder()
        builder._make_dist_version = MagicMock(return_value="19.2.0-1-gabcdef12")
        builder._make_dist = AsyncMock()
        builder._prepare_build_env = lambda: (None, [])
        builder.cmd = AsyncMock()
        builder._build_runtime_image = AsyncMock()

        await builder.compile()

        builder._build_runtime_image.assert_awaited_once_with(
            ceph_version="19.2.0-1-gabcdef12"
        )

    async def test_compile_calls_cpatch_for_binary_patch(self, builder_config):
        builder_config(image_builder="binary-patch")
        builder = CephBuilder()
        builder._prepare_build_env = lambda: (None, [])
        builder.cmd = AsyncMock()
        builder.resolve_distro = AsyncMock()
        builder._build_runtime_image = AsyncMock()
        builder._build_cpatch_image = AsyncMock()

        await builder.compile()

        builder._build_runtime_image.assert_not_awaited()
        builder._build_cpatch_image.assert_awaited_once()

    async def test_build_runtime_image_invokes_build_sh(self, tmp_path, builder_config):
        repo = tmp_path / "ceph"
        (repo / "container").mkdir(parents=True)
        (repo / "container" / "build.sh").write_text("#!/bin/bash\n")
        rpms = repo / "build" / "rpmbuild" / "RPMS" / "x86_64"
        rpms.mkdir(parents=True)
        (rpms / "ceph-base.rpm").write_bytes(b"rpm")

        builder_config(
            repo=str(repo),
            image_builder="package-build",
            build_distro="centos9",
            base_image="localhost/ceph-devstack:test",
        )
        builder = CephBuilder()
        builder.cmd = AsyncMock()

        repo_url = "http://host.containers.internal:8765/custom-ceph.repo"

        @asynccontextmanager
        async def fake_serve(_packages_dir):
            yield repo_url

        with (
            patch(
                "ceph_devstack.resources.ceph.ceph_builder.serve_yum_repo",
                fake_serve,
            ),
            patch(
                "ceph_devstack.resources.ceph.ceph_builder.git_branch",
                return_value="main",
            ),
            patch(
                "ceph_devstack.resources.ceph.ceph_builder.git_sha1",
                return_value="deadbeef",
            ),
        ):
            await builder._build_runtime_image(ceph_version="19.2.0")

        build_call = builder.cmd.await_args_list[0]
        cmd, kwargs = build_call.args[0], build_call.kwargs
        assert cmd[0] == "bash"
        assert cmd[1].endswith("container/build.sh")
        assert kwargs["env"]["CUSTOM_CEPH_REPO_URL"] == repo_url
        assert "CUSTOM_CEPH_PACKAGES_DIR" not in kwargs["env"]
        assert kwargs["env"]["NO_PUSH"] == "true"
        assert kwargs["env"]["BRANCH"] == "main"
        assert kwargs["env"]["CEPH_SHA1"] == "deadbeef"
        tag_cmds = [c.args[0] for c in builder.cmd.await_args_list[1:]]
        assert ["podman", "tag", "build.sh.output", "localhost/ceph-devstack:test"] in (
            tag_cmds
        )


class TestCephBuilderBuildDistro:
    async def test_detect_base_python_parses_version(self, builder_config):
        builder_config(base_image="quay.ceph.io/ceph-ci/ceph:main")
        builder = CephBuilder()
        mock_proc = AsyncMock(returncode=0)
        mock_proc.collect_output = AsyncMock(return_value=("3.12\n", ""))
        builder.cmd = AsyncMock(return_value=mock_proc)

        assert await builder._detect_base_python() == "3.12"
        cmd = builder.cmd.call_args[0][0]
        assert cmd[:5] == ["podman", "run", "--rm", "--entrypoint", "python3"]
        assert cmd[5] == "quay.ceph.io/ceph-ci/ceph:main"

    async def test_detect_base_python_returns_none_on_failure(self, builder_config):
        builder_config()
        builder = CephBuilder()
        builder.run_in_image = AsyncMock(return_value=None)
        assert await builder._detect_base_python() is None

    @pytest.mark.parametrize(
        "configured, python, expected",
        [
            (None, "3.12", "centos10"),
            ("centos9", "3.12", "centos9"),
            (None, "3.9", "centos9"),
        ],
    )
    async def test_resolve_distro(self, builder_config, configured, python, expected):
        overrides = {"build_distro": configured} if configured else {}
        builder_config(**overrides)
        builder = CephBuilder()
        builder.run_in_image = AsyncMock(return_value=f"{python}\n")

        await builder.resolve_distro()

        assert builder.distro == expected
        if configured is None:
            assert "build_distro" not in config["containers"]["ceph_builder"]

    async def test_failed_detection_sticks(self, builder_config):
        builder_config()
        builder = CephBuilder()
        builder.run_in_image = AsyncMock(side_effect=[None, "3.12\n"])
        await builder.resolve_distro()
        await builder.resolve_distro()
        assert builder.distro == "centos9"
        builder.run_in_image.assert_awaited_once()

    @pytest.mark.parametrize(
        "overrides", [{"repo": ""}, {"image_builder": "package-build"}]
    )
    async def test_resolve_distro_skipped(self, builder_config, overrides):
        builder_config(**overrides)
        builder = CephBuilder()
        builder.run_in_image = AsyncMock()
        await builder.resolve_distro()
        builder.run_in_image.assert_not_awaited()
