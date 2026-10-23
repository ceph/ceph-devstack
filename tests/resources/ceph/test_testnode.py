from pathlib import Path
from unittest.mock import patch

from ceph_devstack import config
from ceph_devstack.resources.ceph import TestNode as _TestNode


class TestTestnode:
    def test_testnode_loop_device_count_default_to_one(self):
        testnode = _TestNode("testnode_1")
        assert testnode.loop_device_count == 1

    def test_testnode_create_cmd_includes_related_devices(self, tmp_path):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = _TestNode("testnode_1", data_dir=tmp_path)
        create_cmd = testnode.create_cmd
        assert "--device=/dev/loop0" in create_cmd
        assert "--device=/dev/loop1" in create_cmd
        assert "--device=/dev/loop2" in create_cmd
        assert "--device=/dev/loop3" in create_cmd

    def test_testnode_create_cmd_mounts_run_udev_read_only(self, tmp_path):
        create_cmd = _TestNode("testnode_1", data_dir=tmp_path).create_cmd
        index = create_cmd.index("/run/udev:/run/udev:ro")
        assert create_cmd[index - 1] == "-v"

    def test_testnode_create_cmd_unmasks_proc_for_nested_containers(self, tmp_path):
        create_cmd = _TestNode("testnode_1", data_dir=tmp_path).create_cmd
        index = create_cmd.index("unmask=/proc/*")
        assert create_cmd[index - 1] == "--security-opt"

    def test_testnode_devices_is_based_on_loop_device_count_config(self, tmp_path):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = _TestNode("testnode_1", data_dir=tmp_path)
        assert testnode.loop_device_count == 4
        assert testnode.devices == [
            "/dev/loop0",
            "/dev/loop1",
            "/dev/loop2",
            "/dev/loop3",
        ]

    def test_testnode_reuses_existing_backing_files(self, tmp_path, fake_host):
        image_dir = tmp_path / "disk_images"
        fake_host.add_file(image_dir / "testnode_1-4")
        fake_host.add_file(image_dir / "testnode_1-1")
        config["containers"]["testnode"]["loop_device_count"] = 3
        testnode = _TestNode("testnode_1", data_dir=tmp_path)
        assert testnode.devices == ["/dev/loop1", "/dev/loop4", "/dev/loop0"]

    def test_get_available_dmi_mounts_with_all_files_present(self):
        testnode = _TestNode("testnode_1")
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            mock_exists.return_value = True
            mounts = testnode._get_available_dmi_mounts()
            assert mounts == [
                "-v",
                "/dev/null:/sys/class/dmi/id/board_serial",
                "-v",
                "/dev/null:/sys/class/dmi/id/chassis_serial",
                "-v",
                "/dev/null:/sys/class/dmi/id/product_serial",
            ]

    def test_get_available_dmi_mounts_with_no_files_present(self):
        testnode = _TestNode("testnode_1")
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            mock_exists.return_value = False
            mounts = testnode._get_available_dmi_mounts()
            assert mounts == []

    def test_get_available_dmi_mounts_with_partial_files_present(self):
        testnode = _TestNode("testnode_1")
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            # Only board_serial exists
            mock_exists.side_effect = lambda path: "board_serial" in path
            mounts = testnode._get_available_dmi_mounts()
            assert mounts == [
                "-v",
                "/dev/null:/sys/class/dmi/id/board_serial",
            ]

    def test_create_cmd_includes_dmi_mounts_when_available(self, tmp_path):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = _TestNode("testnode_1", data_dir=tmp_path)
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            mock_exists.return_value = True
            create_cmd = testnode.create_cmd
            assert "/dev/null:/sys/class/dmi/id/board_serial" in " ".join(create_cmd)
            assert "/dev/null:/sys/class/dmi/id/chassis_serial" in " ".join(create_cmd)
            assert "/dev/null:/sys/class/dmi/id/product_serial" in " ".join(create_cmd)

    def test_create_cmd_excludes_dmi_mounts_when_unavailable(self, tmp_path):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = _TestNode("testnode_1", data_dir=tmp_path)
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            mock_exists.return_value = False
            create_cmd = testnode.create_cmd
            cmd_str = " ".join(create_cmd)
            assert "/sys/class/dmi/id/board_serial" not in cmd_str
            assert "/sys/class/dmi/id/chassis_serial" not in cmd_str
            assert "/sys/class/dmi/id/product_serial" not in cmd_str
