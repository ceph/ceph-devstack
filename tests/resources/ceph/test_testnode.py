from pathlib import Path

import pytest

from ceph_devstack import config
from ceph_devstack.resources.ceph import TestNode as _TestNode


class TestTestnode:
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self) -> type[_TestNode]:
        return _TestNode

    def test_testnode_loop_device_count_default_to_one(self, cls):
        testnode = cls("testnode_1")
        assert testnode.loop_device_count == 1

    def test_testnode_create_cmd_includes_related_devices(self, cls, tmp_path):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        config["data_dir"] = str(tmp_path)
        testnode = cls("testnode_1")
        create_cmd = testnode.create_cmd
        assert "--device=/dev/loop0" in create_cmd
        assert "--device=/dev/loop1" in create_cmd
        assert "--device=/dev/loop2" in create_cmd
        assert "--device=/dev/loop3" in create_cmd

    def test_testnode_devices_is_based_on_loop_device_count_config(self, cls, tmp_path):
        config.load(Path(__file__).parent.joinpath("fixtures", "testnode-config.toml"))
        config["data_dir"] = str(tmp_path)
        testnode = cls("testnode_1")
        assert testnode.loop_device_count == 4
        assert testnode.devices == [
            "/dev/loop0",
            "/dev/loop1",
            "/dev/loop2",
            "/dev/loop3",
        ]

    def test_testnode_reuses_existing_backing_files(self, cls, tmp_path):
        config["data_dir"] = str(tmp_path)
        image_dir = tmp_path / "disk_images"
        image_dir.mkdir()
        (image_dir / "testnode_1-4").write_bytes(b"")
        (image_dir / "testnode_1-1").write_bytes(b"")
        config["containers"]["testnode"]["loop_device_count"] = 3
        testnode = cls("testnode_1")
        assert testnode.devices == ["/dev/loop1", "/dev/loop4", "/dev/loop0"]
