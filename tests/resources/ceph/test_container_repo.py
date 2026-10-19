"""Tests for HTTP yum repo helpers used with CUSTOM_CEPH_REPO_URL."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ceph_devstack.resources.ceph.container_repo import (
    CREATEREPO_IMAGE,
    build_sh_env,
    createrepo_podman_cmd,
    ensure_repodata,
    find_rpm_packages_dir,
    pick_free_port,
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
        assert env["FROM_IMAGE"] == "quay.io/centos/centos:stream9"

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
            await ensure_repodata(tmp_path)

        cmd = mock_arun.await_args.args[0]
        assert cmd[0] == "podman"
        assert CREATEREPO_IMAGE in cmd

    @pytest.mark.parametrize(
        "returncode, stdout, expected",
        [(0, "8765\n", 8765), (1, "", None), (0, "oops", None)],
    )
    async def test_pick_free_port(self, returncode, stdout, expected):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=returncode)
        mock_proc.collect_output = AsyncMock(return_value=(stdout, ""))
        with patch(
            "ceph_devstack.resources.ceph.container_repo.host.arun",
            AsyncMock(return_value=mock_proc),
        ):
            if expected is None:
                with pytest.raises(RuntimeError, match="free TCP port"):
                    await pick_free_port()
            else:
                assert await pick_free_port() == expected
