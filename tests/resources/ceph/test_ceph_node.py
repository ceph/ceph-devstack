import json
import re
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ceph_devstack.resources.ceph.ceph_node import (
    CONTAINER_CLUSTER_DIR,
    DASHBOARD_PASSWORD_FILE,
    ENTRYPOINT_SCRIPT,
    CephNode,
)
from ceph_devstack.resources.ceph import containers
from ceph_devstack.resources.ceph.containers import SELINUX_RELABEL_SUFFIX
from ceph_devstack.resources.container import Container


@pytest.fixture
def node(tmp_path, mock_loop_devices) -> CephNode:
    return CephNode(data_dir=tmp_path)


def _proc(returncode: int) -> MagicMock:
    proc = MagicMock(returncode=returncode)
    proc.wait = AsyncMock(return_value=returncode)
    return proc


class TestCephNodeBuild:
    def test_image_uses_configured_value(self, ceph_node_config, node):
        ceph_node_config(image="quay.ceph.io/ceph-ci/ceph:custom")
        assert node.image == "quay.ceph.io/ceph-ci/ceph:custom"

    async def test_build_ignores_registry_image(self, node):
        with patch.object(node, "cmd", new=AsyncMock()) as mock_cmd:
            await node.build()
        mock_cmd.assert_not_awaited()

    @pytest.mark.parametrize("returncode", [0, 1])
    async def test_build_checks_local_image(self, ceph_node_config, node, returncode):
        ceph_node_config(image="localhost/ceph:test")
        with patch.object(node, "cmd", new=AsyncMock(return_value=_proc(returncode))):
            if returncode:
                with pytest.raises(FileNotFoundError, match="localhost/ceph:test"):
                    await node.build()
            else:
                await node.build()
            node.cmd.assert_awaited_once_with(
                ["podman", "image", "exists", "localhost/ceph:test"]
            )


class TestCephNodeLayout:
    def test_disk_images_are_shared_with_testnodes(self, tmp_path, node):
        assert node.loop_img_dir == containers.TestNode(data_dir=tmp_path).loop_img_dir

    def test_cluster_dir_holds_no_disk_images(self, tmp_path, node):
        assert node.cluster_dir == tmp_path / "cluster"
        assert not node.loop_img_dir.is_relative_to(node.cluster_dir)


class TestCephNodeCreateCmd:
    def test_capabilities_are_minimal(self, node):
        # Any change to this list must be justified; see CAPABILITIES.
        cmd = node.create_cmd
        assert cmd[cmd.index("--cap-drop") + 1] == "all"
        assert cmd.count("--cap-add") == 1
        assert set(cmd[cmd.index("--cap-add") + 1].split(",")) == {
            "CHOWN",
            "DAC_OVERRIDE",
            "SETUID",
            "SETGID",
        }
        assert "no-new-privileges" in cmd
        assert "--privileged" not in cmd
        for dropped in ("/dev/fuse", "/dev/net/tun", "/dev/disk"):
            assert not any(dropped in arg for arg in cmd)

    def test_image_is_last_so_env_vars_can_be_added(self, node):
        args = node.add_env_to_args(node.create_cmd)
        assert args[-1] == node.image
        entrypoint = json.loads(args[args.index("--entrypoint") + 1])
        assert entrypoint == [
            "/bin/bash",
            f"{CONTAINER_CLUSTER_DIR}/{ENTRYPOINT_SCRIPT.name}",
        ]

    def test_cluster_mounts_are_labelled(self, tmp_path, node):
        cmd = node.create_cmd
        mounts = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-v"]
        assert (
            f"{tmp_path}/cluster:{CONTAINER_CLUSTER_DIR}{SELINUX_RELABEL_SUFFIX}"
            in (mounts)
        )
        assert (
            f"{tmp_path}/cluster/var/lib/ceph:/var/lib/ceph{SELINUX_RELABEL_SUFFIX}"
            in mounts
        )

    def test_env_comes_from_config_without_the_password(self, ceph_node_config, node):
        ceph_node_config(
            dashboard_port=9000,
            dashboard_ssl="true",
            dashboard_password="s3cret",
        )
        cmd = node.create_cmd
        env = dict(cmd[i + 1].split("=", 1) for i, arg in enumerate(cmd) if arg == "-e")
        assert env["DASHBOARD_PORT"] == "9000"
        assert env["DASHBOARD_SSL"] == "true"
        assert env["DASHBOARD_SHOW_PASSWORD"] == "false"
        assert env["OSD_DEVICES"] == "/dev/loop0,/dev/loop1,/dev/loop2"
        assert not any("s3cret" in arg for arg in cmd)

    def test_entrypoint_tolerates_a_full_dev_disk_and_checks_osds_locally(self):
        script = ENTRYPOINT_SCRIPT.read_text()
        assert "\nmon data avail warn = 10\n" in script
        assert 'ceph_cmd daemon "osd.${id}" status' in script

    def test_entrypoint_requires_exactly_the_env_it_is_given(self, node):
        script = ENTRYPOINT_SCRIPT.read_text()
        required = set(re.findall(r'^\w+="\$\{(\w+):?\?\}"$', script, re.M))
        assert required == set(node.entrypoint_env) - {"IBM_TELEMETRY_DISABLED"}


class TestCephNodeLifecycle:
    @pytest.mark.parametrize("exists", [False, True])
    async def test_create_prepares_only_a_new_container(
        self, ceph_node_config, node, exists
    ):
        ceph_node_config(dashboard_password="s3cret")
        with (
            patch.object(node, "exists", new=AsyncMock(return_value=exists)),
            patch.object(Container, "create", new=AsyncMock()) as mock_create,
            patch.object(node, "create_loop_devices", new=AsyncMock()) as mock_loops,
            patch.object(node, "cmd", new=AsyncMock(return_value=_proc(0))),
        ):
            await node.create()
        mock_create.assert_awaited_once()
        password_file = node.cluster_dir / DASHBOARD_PASSWORD_FILE
        if exists:
            mock_loops.assert_not_awaited()
            assert not node.cluster_dir.exists()
            return
        mock_loops.assert_awaited_once()
        assert (node.cluster_dir / ENTRYPOINT_SCRIPT.name).is_file()
        assert (node.cluster_dir / "var/lib/ceph").is_dir()
        assert password_file.read_text() == "s3cret"
        assert password_file.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize("pool", [None, object()])
    async def test_fresh_create_clears_old_cluster_dir_without_pool(self, node, pool):
        rm = ["podman", "unshare", "rm", "-rf", str(node.cluster_dir)]
        with (
            patch.object(node, "exists", new=AsyncMock(return_value=False)),
            patch.object(Container, "create", new=AsyncMock()),
            patch.object(node, "create_loop_devices", new=AsyncMock()),
            patch.object(node, "cmd", new=AsyncMock(return_value=_proc(0))) as mock_cmd,
            patch(
                "ceph_devstack.resources.ceph.ceph_node.shared_pool", return_value=pool
            ),
        ):
            await node.create()
        assert (call(rm) in mock_cmd.await_args_list) is (pool is None)
        assert (node.cluster_dir / ENTRYPOINT_SCRIPT.name).is_file()

    @pytest.mark.parametrize("returncode", [0, 1])
    async def test_remove_deletes_cluster_dir(self, node, returncode, caplog):
        with (
            patch.object(Container, "remove", new=AsyncMock()) as mock_remove,
            patch.object(node, "remove_loop_devices", new=AsyncMock()) as mock_loops,
            patch.object(
                node, "cmd", new=AsyncMock(return_value=_proc(returncode))
            ) as mock_cmd,
        ):
            await node.remove()
        mock_remove.assert_awaited_once()
        mock_loops.assert_awaited_once()
        mock_cmd.assert_awaited_once_with(
            ["podman", "unshare", "rm", "-rf", str(node.cluster_dir)]
        )
        assert ("failed to remove" in caplog.text) is bool(returncode)
