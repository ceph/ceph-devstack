import json
from unittest.mock import patch

import pytest

from ceph_devstack import config
from ceph_devstack.block_pool import (
    BlockPool,
    BlockPoolError,
    format_size,
    parse_size,
    shared_pool,
    validate_parent_name,
)

MiB = 1024**2
TAIL = 20 * MiB - 4096  # tail marker offset on the default 20 MiB fake parent


def write(fake_host, path: str, offset: int, data: bytes) -> None:
    fake_host.files[path][offset : offset + len(data)] = data


class TestParseSize:
    def test_gigabytes(self):
        assert parse_size("50G") == 50 * 1024**3

    def test_integer(self):
        assert parse_size(1024) == 1024

    @pytest.mark.parametrize("value", ["not-a-size", "0", 0, "0G"])
    def test_invalid(self, value):
        with pytest.raises(BlockPoolError):
            parse_size(value)

    def test_format_size_human_units(self):
        assert format_size(5 * 1024**3) == "5G"
        assert format_size(12345) == "12345B"


class TestValidateParentName:
    @pytest.mark.parametrize("name", ["nvme0n1p1", "nvme0n1", "sda1"])
    def test_accepts_disks_and_partitions(self, fake_host, name):
        parent = fake_host.add_block_device(name)
        assert validate_parent_name(parent) == parent

    def test_resolves_symlinks_on_host(self, fake_host):
        fake_host.add_block_device("nvme0n1p1")
        fake_host.responses["realpath"] = (0, b"/dev/nvme0n1p1\n")
        assert validate_parent_name("/dev/disk/by-id/x") == "/dev/nvme0n1p1"
        assert ["realpath", "/dev/disk/by-id/x"] in fake_host.commands

    def test_rejects_device_mapper(self):
        with pytest.raises(BlockPoolError, match="not allowed"):
            validate_parent_name("/dev/dm-0")

    def test_rejects_non_dev_path(self):
        with pytest.raises(BlockPoolError, match="must be a /dev path"):
            validate_parent_name("/tmp/not-a-device")

    def test_rejects_missing_device(self):
        with pytest.raises(BlockPoolError, match="does not exist"):
            validate_parent_name("/dev/nvme0n1p1")

    def test_rejects_non_block_device(self, fake_host):
        fake_host.add_file("/dev/nvme0n1p1")
        with pytest.raises(BlockPoolError, match="not a block device"):
            validate_parent_name("/dev/nvme0n1p1")

    def test_rejects_unknown_block_name(self, fake_host):
        fake_host.add_file("/dev/foo")
        with pytest.raises(BlockPoolError, match="block device or partition"):
            validate_parent_name("/dev/foo")


class TestBlockPool:
    def test_allocate_and_reuse_stable_owner(self, enrolled_pool):
        first_offset = enrolled_pool.get_or_allocate("ceph_node", 0, MiB)
        second_offset = enrolled_pool.get_or_allocate("ceph_node", 1, MiB)
        again_offset = enrolled_pool.get_or_allocate("ceph_node", 0, MiB)
        assert (first_offset, second_offset, again_offset) == (0, MiB, 0)

    def test_release_and_reuse_slice(self, enrolled_pool):
        enrolled_pool.get_or_allocate("testnode_0", 0, MiB)
        enrolled_pool.get_or_allocate("testnode_0", 1, MiB)
        enrolled_pool.release_owner("testnode_0")
        reused_offset = enrolled_pool.get_or_allocate("ceph_node", 0, MiB)
        assert reused_offset == 0

    def test_different_sizes_pack_sequentially(self, enrolled_pool):
        first_offset = enrolled_pool.get_or_allocate("ceph_node", 0, MiB)
        second_offset = enrolled_pool.get_or_allocate("testnode_0", 0, 2 * MiB)
        assert (first_offset, second_offset) == (0, MiB)

    def test_parent_change_rejected(self, tmp_path, fake_host, enrolled_pool):
        other = fake_host.add_block_device("nvme1n1p1")
        with pytest.raises(BlockPoolError, match="parent changed"):
            BlockPool(tmp_path / "block_pool.json", other, allow_enroll=True)

    def test_enroll_requires_allow_enroll(self, disallow_enroll_pool):
        with pytest.raises(BlockPoolError, match="allow_enroll"):
            disallow_enroll_pool.ensure_ready()

    @pytest.mark.parametrize(
        "cfg_allow_enroll,expected",
        [
            ({}, False),  # defaults to False when omitted
            ({"allow_enroll": False}, False),
            ({"allow_enroll": True}, True),
        ],
    )
    def test_from_config_allow_enroll(
        self, cfg_allow_enroll, expected, tmp_path, pool_parent
    ):
        pool = BlockPool.from_config(
            {
                "block_pool": {
                    "parent": pool_parent,
                    "state_dir": str(tmp_path),
                    **cfg_allow_enroll,
                }
            }
        )
        assert pool is not None
        assert pool.allow_enroll is expected

    def test_state_dir_defaults_to_data_dir(self, tmp_path, pool_parent):
        pool = BlockPool.from_config(
            {"data_dir": str(tmp_path), "block_pool": {"parent": pool_parent}}
        )
        assert pool is not None
        assert pool.state_path == tmp_path / "block_pool.json"

    def test_shared_pool_is_built_once(self, tmp_path, pool_parent):
        config["block_pool"] = {"parent": pool_parent, "state_dir": str(tmp_path)}
        pool = shared_pool()
        assert pool is not None and pool.parent == pool_parent
        assert shared_pool() is pool

    def test_enroll_rejects_non_empty_parent(self, fake_host, block_pool):
        write(fake_host, block_pool.parent, 0, b"DATA")
        with pytest.raises(BlockPoolError, match="not empty"):
            block_pool.ensure_ready()

    def test_enroll_writes_marker_and_state(self, block_pool):
        block_pool.ensure_ready()
        assert block_pool.enrolled
        assert block_pool._read_tail_marker() == {
            "pool_id": block_pool.pool_id,
            "parent": block_pool.parent,
        }

    def test_new_slice_rejects_existing_data(self, fake_host, enrolled_pool):
        write(fake_host, enrolled_pool.parent, 0, b"NOTZEROS")
        with pytest.raises(BlockPoolError, match="not empty"):
            enrolled_pool.get_or_allocate("ceph_node", 0, MiB)

    def test_enroll_rejects_whole_disk_with_partitions(self, tmp_path, fake_host):
        parent = fake_host.add_block_device("nvme0n1")
        fake_host.add_block_device("nvme0n1p1", MiB, disk="nvme0n1")
        pool = BlockPool(tmp_path / "block_pool.json", parent, allow_enroll=True)
        with pytest.raises(BlockPoolError, match="existing partitions"):
            pool.ensure_ready()

    def test_reclaims_marker_after_state_loss(self, disallow_enroll_pool):
        payload = {"pool_id": "saved-pool-id", "parent": disallow_enroll_pool.parent}
        disallow_enroll_pool._write_tail_marker(payload)
        disallow_enroll_pool.ensure_ready()
        assert disallow_enroll_pool.enrolled
        assert disallow_enroll_pool.pool_id == "saved-pool-id"

    def test_reused_slice_must_be_empty(self, fake_host, enrolled_pool):
        enrolled_pool.get_or_allocate("testnode_0", 0, MiB)
        enrolled_pool.release_owner("testnode_0")
        write(fake_host, enrolled_pool.parent, 0, b"STALE")
        with pytest.raises(BlockPoolError, match="not empty"):
            enrolled_pool.get_or_allocate("ceph_node", 0, MiB)

    def test_released_region_is_zeroed_and_reusable(self, fake_host, enrolled_pool):
        parent = enrolled_pool.parent
        assert enrolled_pool.get_or_allocate("testnode_0", 0, MiB) == 0
        write(fake_host, parent, 0, b"BLUESTORE LABEL")  # the owner used it
        fake_host.commands.clear()
        assert enrolled_pool.release_owner("testnode_0") == []
        writes = [c for c in fake_host.commands if any("of=" in a for a in c)]
        assert writes == [
            ["dd", f"of={parent}", "bs=4096", "seek=0", "conv=notrunc,fsync"]
        ]
        assert bytes(fake_host.files[parent][:4096]) == bytes(4096)
        assert enrolled_pool.get_or_allocate("ceph_node", 0, MiB) == 0

    def test_region_that_cannot_be_zeroed_stays_allocated(
        self, fake_host, enrolled_pool
    ):
        enrolled_pool.get_or_allocate("testnode_0", 0, MiB)
        with patch.object(
            enrolled_pool, "_write_parent", side_effect=BlockPoolError("EIO")
        ):
            assert enrolled_pool.release_owner("testnode_0") == ["testnode_0:0"]
        assert list(enrolled_pool.allocations) == ["testnode_0:0"]
        assert enrolled_pool.free_regions == []

    @pytest.mark.parametrize("marker", ["missing", "foreign", "unenrolled"])
    def test_release_writes_nothing_unless_parent_is_ours(
        self, fake_host, enrolled_pool, marker
    ):
        enrolled_pool.get_or_allocate("testnode_0", 0, MiB)
        enrolled_pool.get_or_allocate("testnode_0", 1, MiB)
        if marker == "missing":
            write(fake_host, enrolled_pool.parent, TAIL, bytes(4096))
        elif marker == "foreign":
            enrolled_pool._write_tail_marker(
                {"parent": enrolled_pool.parent, "pool_id": "other-pool"}
            )
        else:
            state = json.loads(enrolled_pool.state_path.read_text())
            state["enrolled"] = False
            enrolled_pool.state_path.write_text(json.dumps(state))
        write(fake_host, enrolled_pool.parent, 0, b"FOREIGN")
        fake_host.commands.clear()
        kept = enrolled_pool.release_owner("testnode_0")
        assert kept == ["testnode_0:0", "testnode_0:1"]
        assert not [c for c in fake_host.commands if any("of=" in a for a in c)]
        assert bytes(fake_host.files[enrolled_pool.parent][:7]) == b"FOREIGN"
        assert sorted(enrolled_pool.allocations) == kept


class TestBlockPoolEnrollmentGuards:
    def test_enroll_rejects_mounted_parent(self, fake_host, block_pool):
        fake_host.responses["findmnt"] = (0, b"/mnt\n")
        with pytest.raises(BlockPoolError, match="mounted"):
            block_pool.ensure_ready()
        assert [
            "findmnt",
            "-n",
            "-o",
            "TARGET",
            "--source",
            block_pool.parent,
        ] in fake_host.commands

    def test_enroll_rejects_blkid_signature(self, fake_host, block_pool):
        fake_host.responses["blkid"] = (0, b"DEVNAME=x\nTYPE=xfs\n")
        with pytest.raises(BlockPoolError, match="filesystem or partition"):
            block_pool.ensure_ready()

    def test_verify_rejects_missing_marker(self, fake_host, enrolled_pool):
        write(fake_host, enrolled_pool.parent, TAIL, bytes(4096))
        with pytest.raises(BlockPoolError, match="missing the devstack"):
            enrolled_pool.ensure_ready()

    def test_verify_rejects_foreign_pool_id(self, enrolled_pool):
        enrolled_pool._write_tail_marker(
            {"parent": enrolled_pool.parent, "pool_id": "other-pool"}
        )
        with pytest.raises(BlockPoolError, match="belongs to another pool"):
            enrolled_pool.ensure_ready()

    def test_region_exceeds_parent_size(self, enrolled_pool):
        with pytest.raises(BlockPoolError, match="exceeds"):
            enrolled_pool.get_or_allocate("ceph_node", 0, 32 * MiB)


class TestHasPartitions:
    def test_whole_disk_with_partitions(self, fake_host):
        fake_host.add_block_device("nvme0n1")
        fake_host.add_block_device("nvme0n1p1", disk="nvme0n1")
        assert BlockPool._has_partitions("nvme0n1") is True

    @pytest.mark.parametrize(
        "disk,other_disk,other_partition",
        [("nvme0n1", "nvme0n11", "nvme0n11p1"), ("sda", "sdaa", "sdaa1")],
    )
    def test_partitions_of_similarly_named_disks_do_not_count(
        self, fake_host, disk, other_disk, other_partition
    ):
        fake_host.add_block_device(disk)
        fake_host.add_block_device(other_disk)
        fake_host.add_block_device(other_partition, disk=other_disk)
        assert BlockPool._has_partitions(disk) is False

    def test_partition_has_no_partitions(self):
        assert BlockPool._has_partitions("nvme0n1p1") is False


class TestTailMarker:
    def test_roundtrip(self, block_pool):
        payload = {"pool_id": "abc", "parent": block_pool.parent}
        block_pool._write_tail_marker(payload)
        assert block_pool._read_tail_marker() == payload

    def test_corrupt_marker_returns_none(self, fake_host, block_pool):
        write(
            fake_host, block_pool.parent, TAIL, b"CEPH-DEVSTACK-BLOCK-POOL-TAIL-v1\x00x"
        )
        assert block_pool._read_tail_marker() is None
