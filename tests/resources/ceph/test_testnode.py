from pathlib import Path
from unittest.mock import patch

import pytest

from ceph_devstack.resources.ceph import TestNode as _TestNode
from ceph_devstack import config


class TestTestnode:
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self) -> type[_TestNode]:
        return _TestNode

    def test_testnode_loop_device_count_default_to_one(self, cls):
        testnode = cls("testnode_1")
        assert testnode.loop_device_count == 1

    def test_testnode_create_cmd_includes_related_devices(self, cls):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = cls("testnode_1")
        create_cmd = testnode.create_cmd
        assert "--device=/dev/loop4" in create_cmd
        assert "--device=/dev/loop5" in create_cmd
        assert "--device=/dev/loop6" in create_cmd
        assert "--device=/dev/loop7" in create_cmd

    def test_testnode_devices_is_based_on_loop_device_count_config(self, cls):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = cls("testnode_1")
        assert testnode.loop_device_count == 4
        assert testnode.devices == [
            "/dev/loop4",
            "/dev/loop5",
            "/dev/loop6",
            "/dev/loop7",
        ]

    def test_get_available_dmi_mounts_with_all_files_present(self, cls):
        testnode = cls("testnode_1")
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

    def test_get_available_dmi_mounts_with_no_files_present(self, cls):
        testnode = cls("testnode_1")
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            mock_exists.return_value = False
            mounts = testnode._get_available_dmi_mounts()
            assert mounts == []

    def test_get_available_dmi_mounts_with_partial_files_present(self, cls):
        testnode = cls("testnode_1")
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

    def test_create_cmd_includes_dmi_mounts_when_available(self, cls):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = cls("testnode_1")
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            mock_exists.return_value = True
            create_cmd = testnode.create_cmd
            assert "/dev/null:/sys/class/dmi/id/board_serial" in " ".join(create_cmd)
            assert "/dev/null:/sys/class/dmi/id/chassis_serial" in " ".join(create_cmd)
            assert "/dev/null:/sys/class/dmi/id/product_serial" in " ".join(create_cmd)

    def test_create_cmd_excludes_dmi_mounts_when_unavailable(self, cls):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        testnode = cls("testnode_1")
        with patch(
            "ceph_devstack.resources.ceph.containers.host.path_exists"
        ) as mock_exists:
            mock_exists.return_value = False
            create_cmd = testnode.create_cmd
            cmd_str = " ".join(create_cmd)
            assert "/sys/class/dmi/id/board_serial" not in cmd_str
            assert "/sys/class/dmi/id/chassis_serial" not in cmd_str
            assert "/sys/class/dmi/id/product_serial" not in cmd_str
