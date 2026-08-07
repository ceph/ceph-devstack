"""Tests for HTTP yum repo helpers used with CUSTOM_CEPH_REPO_URL."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceph_devstack import DEFAULT_CEPH_IMAGE, config
from ceph_devstack.resources.ceph.ceph_builder import CephBuilder
from ceph_devstack.resources.ceph.ceph_node import CephNode
from ceph_devstack.resources.ceph.container_repo import (
    CREATEREPO_IMAGE,
    build_sh_env,
    createrepo_podman_cmd,
    find_rpm_packages_dir,
    from_image_for_distro,
    runtime_image_tag,
    serve_yum_repo,
    write_custom_repo,
)


class TestContainerRepoHelpers:
    def test_find_rpm_packages_dir_prefers_build_subdir(self, tmp_path):
        rpms = tmp_path / "build" / "rpmbuild" / "RPMS" / "x86_64"
        rpms.mkdir(parents=True)
        (rpms / "ceph-base-1.rpm").write_bytes(b"rpm")
        assert find_rpm_packages_dir(tmp_path, "build") == (
            tmp_path / "build" / "rpmbuild" / "RPMS"
        )

    def test_find_rpm_packages_dir_falls_back_to_repo_rpmbuild(self, tmp_path):
        rpms = tmp_path / "rpmbuild" / "RPMS" / "noarch"
        rpms.mkdir(parents=True)
        (rpms / "ceph-common-1.rpm").write_bytes(b"rpm")
        assert find_rpm_packages_dir(tmp_path, "build") == (
            tmp_path / "rpmbuild" / "RPMS"
        )

    def test_find_rpm_packages_dir_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No RPMs found"):
            find_rpm_packages_dir(tmp_path, "build")

    def test_runtime_image_tag_keeps_localhost(self):
        assert (
            runtime_image_tag("localhost/ceph-devstack:custom")
            == "localhost/ceph-devstack:custom"
        )

    def test_runtime_image_tag_rewrites_remote(self):
        assert (
            runtime_image_tag(DEFAULT_CEPH_IMAGE, branch="feature/x")
            == "localhost/ceph-devstack:x"
        )

    def test_runtime_image_tag_binary_patch_uses_cpatch_name(self):
        assert (
            runtime_image_tag(
                DEFAULT_CEPH_IMAGE,
                branch="feature/x",
                image_builder="binary-patch",
            )
            == "localhost/ceph-cpatch:x"
        )

    def test_from_image_for_distro(self):
        assert "stream9" in from_image_for_distro("centos9")
        assert "rockylinux:10" in from_image_for_distro("centos10")

    def test_write_custom_repo(self, tmp_path):
        path = write_custom_repo(tmp_path, "http://host.containers.internal:9999")
        text = path.read_text(encoding="utf-8")
        assert path.name == "custom-ceph.repo"
        assert "baseurl=http://host.containers.internal:9999/" in text
        assert "gpgcheck=0" in text
        assert "127.0.0.1" not in text
        assert "localhost" not in text

    def test_write_custom_repo_rejects_loopback(self, tmp_path):
        with pytest.raises(ValueError, match="loopback"):
            write_custom_repo(tmp_path, "http://127.0.0.1:8080/")

    def test_build_sh_env(self):
        env = build_sh_env(
            custom_ceph_repo_url="http://host.containers.internal:9/custom-ceph.repo",
            distro="centos9",
            branch="main",
            sha1="abc",
            version="19.2.0",
        )
        assert (
            env["CUSTOM_CEPH_REPO_URL"]
            == "http://host.containers.internal:9/custom-ceph.repo"
        )
        assert "CUSTOM_CEPH_PACKAGES_DIR" not in env
        assert env["NO_PUSH"] == "true"
        assert env["REMOVE_LOCAL_IMAGES"] == "false"
        assert env["CI_CONTAINER"] == "true"
        assert env["BRANCH"] == "main"
        assert env["CEPH_SHA1"] == "abc"
        assert env["VERSION"] == "19.2.0"

    def test_createrepo_podman_cmd_shape(self, tmp_path):
        cmd = createrepo_podman_cmd(tmp_path)
        assert cmd[0] == "podman"
        assert "run" in cmd
        assert CREATEREPO_IMAGE in cmd
        assert f"{tmp_path}:/repo:Z" in cmd
        assert "createrepo_c" in " ".join(cmd)


class TestServeYumRepo:
    async def test_serve_yum_repo_uses_host_arun_python3(self, tmp_path):
        (tmp_path / "repodata").mkdir()
        (tmp_path / "ceph.rpm").write_bytes(b"rpm")

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stdout = None

        with (
            patch(
                "ceph_devstack.resources.ceph.container_repo.pick_free_port",
                AsyncMock(return_value=8765),
            ),
            patch(
                "ceph_devstack.resources.ceph.container_repo.host.arun",
                AsyncMock(return_value=mock_proc),
            ) as mock_arun,
            patch(
                "ceph_devstack.resources.ceph.container_repo.asyncio.sleep", AsyncMock()
            ),
        ):
            async with serve_yum_repo(tmp_path) as repo_url:
                assert (
                    repo_url == "http://host.containers.internal:8765/custom-ceph.repo"
                )
                assert "127.0.0.1" not in repo_url
                assert "localhost" not in repo_url
                server_cmd = mock_arun.await_args.args[0]
                assert server_cmd[0] == "python3"
                assert server_cmd[1:3] == ["-m", "http.server"]
                assert "--directory" in server_cmd
                assert str(tmp_path) in server_cmd
                # Never Mac-local interpreter path
                assert not server_cmd[0].endswith("python")
                assert "sys.executable" not in str(server_cmd)

            mock_proc.terminate.assert_called()

        repo_file = tmp_path / "custom-ceph.repo"
        assert repo_file.is_file()
        assert "baseurl=http://host.containers.internal:8765/" in repo_file.read_text()

    async def test_ensure_repodata_uses_podman_when_missing(self, tmp_path):
        (tmp_path / "ceph.rpm").write_bytes(b"rpm")
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stdout = None

        with patch(
            "ceph_devstack.resources.ceph.container_repo.host.arun",
            AsyncMock(return_value=mock_proc),
        ) as mock_arun:
            from ceph_devstack.resources.ceph.container_repo import ensure_repodata

            await ensure_repodata(tmp_path)

        cmd = mock_arun.await_args.args[0]
        assert cmd[0] == "podman"
        assert CREATEREPO_IMAGE in cmd


class TestCephBuilderRuntimeImage:
    async def test_compile_triggers_runtime_image_for_package_build(self, tmp_path):
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {
            "repo": str(repo),
            "image_builder": "package-build",
            "build_distro": "centos9",
            "sccache": False,
            "npm_cache": False,
        }
        builder = CephBuilder()
        builder._make_dist_version = AsyncMock(return_value="19.2.0-1-gabcdef12")
        builder._make_dist = AsyncMock()
        builder._prepare_build_env = lambda: (None, [])
        builder.cmd = AsyncMock()
        builder._build_runtime_image = AsyncMock()

        await builder.compile()

        builder._build_runtime_image.assert_awaited_once_with(
            ceph_version="19.2.0-1-gabcdef12"
        )

    async def test_compile_calls_cpatch_for_binary_patch(self, tmp_path):
        repo = tmp_path / "ceph"
        repo.mkdir()
        config["containers"]["ceph_builder"] = {
            "repo": str(repo),
            "image_builder": "binary-patch",
            "sccache": False,
            "npm_cache": False,
        }
        builder = CephBuilder()
        builder._prepare_build_env = lambda: (None, [])
        builder.cmd = AsyncMock()
        builder._resolve_build_distro = AsyncMock()
        builder._build_runtime_image = AsyncMock()
        builder._build_cpatch_image = AsyncMock()

        await builder.compile()

        builder._build_runtime_image.assert_not_awaited()
        builder._build_cpatch_image.assert_awaited_once()

    async def test_build_runtime_image_invokes_build_sh(self, tmp_path):
        repo = tmp_path / "ceph"
        (repo / "container").mkdir(parents=True)
        (repo / "container" / "build.sh").write_text("#!/bin/bash\n")
        rpms = repo / "build" / "rpmbuild" / "RPMS" / "x86_64"
        rpms.mkdir(parents=True)
        (rpms / "ceph-base.rpm").write_bytes(b"rpm")

        config["containers"]["ceph_builder"] = {
            "repo": str(repo),
            "image_builder": "package-build",
            "build_distro": "centos9",
            "target_image": "localhost/ceph-devstack:test",
        }
        builder = CephBuilder()
        builder._git_value = AsyncMock(side_effect=["main", "deadbeef"])
        builder.cmd = AsyncMock()

        repo_url = "http://host.containers.internal:8765/custom-ceph.repo"

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_serve(_packages_dir):
            yield repo_url

        with patch(
            "ceph_devstack.resources.ceph.ceph_builder.serve_yum_repo",
            fake_serve,
        ):
            await builder._build_runtime_image(ceph_version="19.2.0")

        build_call = builder.cmd.await_args_list[0]
        cmd, kwargs = build_call.args[0], build_call.kwargs
        assert cmd[0] == "bash"
        assert cmd[1].endswith("container/build.sh")
        assert kwargs["env"]["CUSTOM_CEPH_REPO_URL"] == repo_url
        assert "CUSTOM_CEPH_PACKAGES_DIR" not in kwargs["env"]
        assert kwargs["env"]["NO_PUSH"] == "true"
        tag_cmds = [c.args[0] for c in builder.cmd.await_args_list[1:]]
        assert ["podman", "tag", "build.sh.output", "localhost/ceph-devstack:test"] in (
            tag_cmds
        )


@pytest.mark.parametrize("image_builder", ["package-build", "binary-patch"])
class TestCephNodeVerifyLocalImage:
    async def test_verifies_local_image(self, image_builder, ceph_node_config):
        ceph_node_config(
            image="localhost/ceph-devstack:main", image_builder=image_builder
        )
        node = CephNode()
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        with patch(
            "ceph_devstack.resources.ceph.ceph_node.host.arun", return_value=mock_proc
        ):
            await node._verify_local_image()

    async def test_missing_image_raises(self, image_builder, ceph_node_config):
        ceph_node_config(
            image="localhost/ceph-devstack:missing", image_builder=image_builder
        )
        node = CephNode()
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)
        with (
            patch(
                "ceph_devstack.resources.ceph.ceph_node.host.arun",
                return_value=mock_proc,
            ),
            pytest.raises(FileNotFoundError, match="requires image"),
        ):
            await node._verify_local_image()

    async def test_skips_non_local_image(self, image_builder, ceph_node_config):
        ceph_node_config(image_builder=image_builder)
        node = CephNode()
        with patch("ceph_devstack.resources.ceph.ceph_node.host.arun") as mock_arun:
            await node._verify_local_image()
            mock_arun.assert_not_called()
