from pathlib import Path

import pytest

from ceph_devstack.resources.ceph import TestNode as _TestNode
from ceph_devstack import config


class TestTestnode:
    @pytest.fixture(scope="class")
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
