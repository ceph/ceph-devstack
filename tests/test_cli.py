import sys
from unittest.mock import patch

from ceph_devstack import config, parse_args
from ceph_devstack.block_pool import BlockPool


class TestBlockPoolCLI:
    def test_parse_args_block_pool_status(self):
        args = parse_args(["block-pool", "status"])
        assert args.command == "block-pool"
        assert args.block_pool_op == "status"

    def test_main_block_pool_status(self):
        from ceph_devstack.cli import main

        with (
            patch.object(sys, "argv", ["ceph-devstack", "block-pool", "status"]),
            patch.object(
                BlockPool, "status_from_config", return_value=0
            ) as mock_status,
        ):
            rc = main()
        assert rc == 0
        mock_status.assert_called_once_with(config)
