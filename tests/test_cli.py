import sys
from unittest.mock import patch

import pytest

from ceph_devstack import config, parse_args
from ceph_devstack.cli import block_pool_status, main


class TestBlockPoolCLI:
    def test_parse_args_block_pool_status(self):
        args = parse_args(["block-pool", "status"])
        assert args.command == "block-pool"
        assert args.block_pool_op == "status"

    @pytest.mark.parametrize("argv,rc", [(["status"], 0), ([], 2)])
    def test_main_dispatches_block_pool_ops(self, argv, rc):
        with (
            patch.object(sys, "argv", ["ceph-devstack", "block-pool", *argv]),
            patch.object(type(config), "load"),
            patch("ceph_devstack.cli.block_pool_status", return_value=0) as status,
        ):
            assert main() == rc
        assert status.call_count == (rc == 0)

    def test_status_disabled(self, caplog):
        with caplog.at_level("INFO", logger="ceph-devstack"):
            assert block_pool_status() == 0
        assert "disabled" in caplog.text

    def test_status_invalid_parent_fails(self, caplog):
        config["block_pool"] = {"parent": "/dev/dm-0"}
        assert block_pool_status() == 1
        assert "not allowed" in caplog.text

    def test_status_shows_allocations(self, tmp_path, enrolled_pool, caplog):
        enrolled_pool.get_or_allocate("ceph_node", 0, 1024**2)
        enrolled_pool.get_or_allocate("testnode_0", 0, 1024**2)
        enrolled_pool.release_owner("testnode_0")
        config["block_pool"] = {
            "parent": enrolled_pool.parent,
            "state_dir": str(tmp_path),
        }
        with caplog.at_level("INFO", logger="ceph-devstack"):
            assert block_pool_status() == 0
        assert f"pool id: {enrolled_pool.pool_id}" in caplog.text
        assert "ceph_node:0: offset 0, size 1M, last owner ceph_node" in caplog.text
        assert "offset 1048576, size 1M" in caplog.text
        assert "next offset: 2097152" in caplog.text
