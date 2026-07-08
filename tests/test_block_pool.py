from pathlib import Path
from unittest.mock import patch

import pytest

from ceph_devstack.block_pool import (
    BlockPool,
    BlockPoolError,
    format_size,
    parse_size,
    validate_parent_name,
    _has_partition_siblings,
    _read_tail_marker,
    _write_tail_marker,
)


class TestParseSize:
    def test_gigabytes(self):
        assert parse_size("50G") == 50 * 1024**3

    def test_integer(self):
        assert parse_size(1024) == 1024

    def test_invalid(self):
        with pytest.raises(BlockPoolError):
            parse_size("not-a-size")

    def test_format_size_human_units(self):
        assert format_size(5 * 1024**3) == "5G"
        assert format_size(12345) == "12345B"


class TestValidateParentName:
    def test_accepts_partition(self):
        with (
            patch(
                "ceph_devstack.block_pool.canonical_device_path",
                return_value="/dev/nvme0n1p1",
            ),
            patch("ceph_devstack.block_pool.os.path.exists", return_value=True),
        ):
            assert validate_parent_name("/dev/nvme0n1p1") == "/dev/nvme0n1p1"

    def test_accepts_whole_disk(self):
        with (
            patch(
                "ceph_devstack.block_pool.canonical_device_path",
                return_value="/dev/nvme0n1",
            ),
            patch("ceph_devstack.block_pool.os.path.exists", return_value=True),
        ):
            assert validate_parent_name("/dev/nvme0n1") == "/dev/nvme0n1"

    def test_rejects_device_mapper(self):
        with pytest.raises(BlockPoolError, match="not allowed"):
            validate_parent_name("/dev/dm-0")

    def test_rejects_non_dev_path(self):
        with (
            patch(
                "ceph_devstack.block_pool.canonical_device_path",
                return_value="/tmp/not-a-device",
            ),
            pytest.raises(BlockPoolError, match="must be a /dev path"),
        ):
            validate_parent_name("/tmp/not-a-device")

    def test_rejects_missing_device(self):
        with (
            patch(
                "ceph_devstack.block_pool.canonical_device_path",
                return_value="/dev/nvme0n1p1",
            ),
            patch("ceph_devstack.block_pool.os.path.exists", return_value=False),
            pytest.raises(BlockPoolError, match="does not exist"),
        ):
            validate_parent_name("/dev/nvme0n1p1")

    def test_rejects_unknown_block_name(self):
        with (
            patch(
                "ceph_devstack.block_pool.canonical_device_path",
                return_value="/dev/foo",
            ),
            patch("ceph_devstack.block_pool.os.path.exists", return_value=True),
            pytest.raises(BlockPoolError, match="block device or partition"),
        ):
            validate_parent_name("/dev/foo")


class TestBlockPool:
    def test_allocate_and_reuse_stable_owner(self, enrolled_pool):
        size = parse_size("10G")
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch.object(enrolled_pool, "_validate_region"),
        ):
            first_offset, _ = enrolled_pool.get_or_allocate("ceph_node", 0, size)
            second_offset, _ = enrolled_pool.get_or_allocate("ceph_node", 1, size)
            again_offset, _ = enrolled_pool.get_or_allocate("ceph_node", 0, size)
        assert again_offset == first_offset
        assert first_offset == 0
        assert second_offset == size

    def test_release_and_reuse_slice(self, enrolled_pool):
        size = parse_size("10G")
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch.object(enrolled_pool, "_validate_region"),
        ):
            enrolled_pool.get_or_allocate("testnode_0", 0, size)
            enrolled_pool.get_or_allocate("testnode_0", 1, size)
        enrolled_pool.release_owner("testnode_0")

        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch.object(enrolled_pool, "_validate_region"),
        ):
            reused_offset, _ = enrolled_pool.get_or_allocate("ceph_node", 0, size)
        assert reused_offset in {0, size}

    def test_different_sizes_pack_sequentially(self, enrolled_pool):
        small = parse_size("5G")
        large = parse_size("10G")
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch.object(enrolled_pool, "_validate_region"),
        ):
            first_offset, _ = enrolled_pool.get_or_allocate("ceph_node", 0, small)
            second_offset, _ = enrolled_pool.get_or_allocate("testnode_0", 0, large)
        assert first_offset == 0
        assert second_offset == small

    def test_parent_change_rejected(self, tmp_path, enrolled_pool):
        size = parse_size("10G")
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch.object(enrolled_pool, "_validate_region"),
        ):
            enrolled_pool.get_or_allocate("ceph_node", 0, size)
        with (
            patch(
                "ceph_devstack.block_pool.validate_parent_name",
                return_value="/dev/nvme1n1p1",
            ),
            pytest.raises(BlockPoolError, match="parent changed"),
        ):
            BlockPool(
                tmp_path / "block_pool.json",
                "/dev/nvme1n1p1",
                allow_enroll=True,
            )

    def test_enroll_requires_allow_enroll(self, disallow_enroll_pool):
        with pytest.raises(BlockPoolError, match="allow_enroll"):
            disallow_enroll_pool.ensure_ready()

    def test_from_config_allow_enroll_defaults_false(self, tmp_path):
        with patch(
            "ceph_devstack.block_pool.validate_parent_name",
            return_value="/dev/nvme0n1p1",
        ):
            pool = BlockPool.from_config(
                {
                    "block_pool": {
                        "parent": "/dev/nvme0n1p1",
                        "state_dir": str(tmp_path),
                    }
                }
            )
        assert pool is not None
        assert pool.allow_enroll is False

    def test_from_config_allow_enroll_explicit_false(self, tmp_path):
        with patch(
            "ceph_devstack.block_pool.validate_parent_name",
            return_value="/dev/nvme0n1p1",
        ):
            pool = BlockPool.from_config(
                {
                    "block_pool": {
                        "parent": "/dev/nvme0n1p1",
                        "state_dir": str(tmp_path),
                        "allow_enroll": False,
                    }
                }
            )
        assert pool is not None
        assert pool.allow_enroll is False

    def test_from_config_allow_enroll_true(self, tmp_path):
        with patch(
            "ceph_devstack.block_pool.validate_parent_name",
            return_value="/dev/nvme0n1p1",
        ):
            pool = BlockPool.from_config(
                {
                    "block_pool": {
                        "parent": "/dev/nvme0n1p1",
                        "state_dir": str(tmp_path),
                        "allow_enroll": True,
                    }
                }
            )
        assert pool is not None
        assert pool.allow_enroll is True

    def test_enroll_rejects_non_empty_parent(self, block_pool, mock_fresh_enrollment):
        with open(block_pool.parent, "r+b") as handle:
            handle.write(b"DATA")
        with pytest.raises(BlockPoolError, match="not empty"):
            block_pool.ensure_ready()

    def test_enroll_writes_marker_and_state(self, block_pool, mock_fresh_enrollment):
        block_pool.ensure_ready()
        assert block_pool.enrolled
        marker = _read_tail_marker(block_pool.parent)
        assert marker is not None
        assert marker["pool_id"] == block_pool._state["pool_id"]
        assert marker["parent"] == block_pool.parent

    def test_new_slice_rejects_existing_data(self, enrolled_pool):
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch(
                "ceph_devstack.block_pool._read_device",
                return_value=b"NOTZEROS" + b"\x00" * 4088,
            ),
            pytest.raises(BlockPoolError, match="not empty"),
        ):
            enrolled_pool.get_or_allocate("ceph_node", 0, 1024 * 1024)

    def test_enroll_rejects_whole_disk_with_partitions(self, tmp_path):
        state_path = tmp_path / "block_pool.json"
        parent = tmp_path / "nvme0n1"
        parent.write_bytes(b"\x00" * (20 * 1024**2))
        with patch(
            "ceph_devstack.block_pool.validate_parent_name",
            return_value=str(parent),
        ):
            pool = BlockPool(state_path, str(parent), allow_enroll=True)
        with (
            patch("ceph_devstack.block_pool._device_mounted", return_value=False),
            patch(
                "ceph_devstack.block_pool._device_has_blkid_signature",
                return_value=False,
            ),
            patch("ceph_devstack.block_pool._read_tail_marker", return_value=None),
            patch(
                "ceph_devstack.block_pool._has_partition_siblings",
                return_value=True,
            ),
            pytest.raises(BlockPoolError, match="existing partitions"),
        ):
            pool.ensure_ready()

    def test_reclaims_marker_after_state_loss(self, disallow_enroll_pool):
        payload = {
            "pool_id": "saved-pool-id",
            "parent": disallow_enroll_pool.parent,
        }
        _write_tail_marker(disallow_enroll_pool.parent, payload)
        with (
            patch("ceph_devstack.block_pool._device_mounted", return_value=False),
            patch(
                "ceph_devstack.block_pool._device_has_blkid_signature",
                return_value=False,
            ),
        ):
            disallow_enroll_pool.ensure_ready()
        assert disallow_enroll_pool.enrolled
        assert disallow_enroll_pool._state["pool_id"] == "saved-pool-id"

    def test_reused_slice_must_be_empty(self, enrolled_pool):
        size = 1024 * 1024
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch("ceph_devstack.block_pool._read_device", return_value=b"\x00" * 4096),
        ):
            enrolled_pool.get_or_allocate("testnode_0", 0, size)
        enrolled_pool.release_owner("testnode_0")
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch(
                "ceph_devstack.block_pool._read_device",
                return_value=b"STALE" + b"\x00" * 4089,
            ),
            pytest.raises(BlockPoolError, match="not empty"),
        ):
            enrolled_pool.get_or_allocate("ceph_node", 0, size)


class TestBlockPoolStatus:
    def test_status_disabled(self, caplog):
        with caplog.at_level("INFO", logger="ceph-devstack"):
            assert BlockPool.status_from_config({}) == 0
        assert "disabled" in caplog.text

    def test_status_shows_allocations(self, tmp_path, enrolled_pool, caplog):
        enrolled_pool._state["allocations"] = {
            "ceph_node:0": {"offset": 0, "size": 1024 * 1024, "owner": "ceph_node"}
        }
        enrolled_pool._save()
        with (
            patch(
                "ceph_devstack.block_pool.validate_parent_name",
                return_value=enrolled_pool.parent,
            ),
            caplog.at_level("INFO", logger="ceph-devstack"),
        ):
            BlockPool.status_from_config(
                {
                    "block_pool": {
                        "parent": enrolled_pool.parent,
                        "state_dir": str(tmp_path),
                    }
                }
            )
        assert "ceph_node:0" in caplog.text
        assert "ceph_node" in caplog.text


class TestBlockPoolEnrollmentGuards:
    def test_enroll_rejects_mounted_parent(self, block_pool, mock_fresh_enrollment):
        with (
            patch("ceph_devstack.block_pool._device_mounted", return_value=True),
            pytest.raises(BlockPoolError, match="mounted"),
        ):
            block_pool.ensure_ready()

    def test_enroll_rejects_blkid_signature(self, block_pool, mock_fresh_enrollment):
        with (
            patch(
                "ceph_devstack.block_pool._device_has_blkid_signature",
                return_value=True,
            ),
            pytest.raises(BlockPoolError, match="filesystem or partition"),
        ):
            block_pool.ensure_ready()

    def test_verify_rejects_missing_marker(self, enrolled_pool):
        with (
            patch("ceph_devstack.block_pool._read_tail_marker", return_value=None),
            pytest.raises(BlockPoolError, match="missing the devstack"),
        ):
            enrolled_pool.ensure_ready()

    def test_verify_rejects_foreign_pool_id(self, enrolled_pool):
        with (
            patch(
                "ceph_devstack.block_pool._read_tail_marker",
                return_value={
                    "parent": enrolled_pool.parent,
                    "pool_id": "other-pool",
                },
            ),
            pytest.raises(BlockPoolError, match="belongs to another pool"),
        ):
            enrolled_pool.ensure_ready()

    def test_region_exceeds_parent_size(self, enrolled_pool):
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch(
                "ceph_devstack.block_pool._device_size_bytes",
                return_value=512 * 1024,
            ),
            pytest.raises(BlockPoolError, match="exceeds"),
        ):
            enrolled_pool.get_or_allocate("ceph_node", 0, 1024 * 1024)

    def test_legacy_slice_size_state_rejected_on_load(self, enrolled_pool):
        enrolled_pool._state["slice_size"] = 1024 * 1024
        enrolled_pool._save()
        with (
            patch(
                "ceph_devstack.block_pool.validate_parent_name",
                return_value=enrolled_pool.parent,
            ),
            pytest.raises(BlockPoolError, match="legacy block pool state"),
        ):
            BlockPool(enrolled_pool.state_path, enrolled_pool.parent)


class TestPartitionSiblings:
    def test_whole_disk_with_nvme_partitions(self):
        class _Entry:
            def __init__(self, name: str):
                self.name = name

            def is_dir(self) -> bool:
                return True

        class _BlockDir:
            def is_dir(self) -> bool:
                return True

            def iterdir(self) -> list[_Entry]:
                return [_Entry("nvme0n1"), _Entry("nvme0n1p1")]

        mock_block_dir = _BlockDir()

        def path_for(arg: str):
            if arg == "/sys/class/block":
                return mock_block_dir
            return Path(arg)

        with patch("ceph_devstack.block_pool.Path", side_effect=path_for):
            assert _has_partition_siblings("nvme0n1") is True

    def test_partition_name_has_no_siblings(self):
        assert _has_partition_siblings("nvme0n1p1") is False


class TestBlockPoolAllocation:
    def test_allocation_for_missing(self, enrolled_pool):
        assert enrolled_pool.allocation_for("nobody", 0) is None

    def test_allocation_for_active(self, enrolled_pool):
        size = 1024 * 1024
        with (
            patch.object(enrolled_pool, "_verify_enrolled_parent"),
            patch("ceph_devstack.block_pool._read_device", return_value=b"\x00" * 4096),
        ):
            enrolled_pool.get_or_allocate("ceph_node", 0, size)
        assert enrolled_pool.allocation_for("ceph_node", 0) == (0, size, size)


class TestTailMarker:
    def test_roundtrip(self, tmp_path):
        parent = tmp_path / "disk"
        parent.write_bytes(b"\x00" * (1024 * 1024))
        payload = {"pool_id": "abc", "parent": str(parent)}
        _write_tail_marker(str(parent), payload)
        assert _read_tail_marker(str(parent)) == payload

    def test_corrupt_marker_returns_none(self, tmp_path):
        parent = tmp_path / "disk"
        parent.write_bytes(b"\x00" * (1024 * 1024))
        offset = len(parent.read_bytes()) - 4096
        with open(parent, "r+b") as handle:
            handle.seek(offset)
            handle.write(b"CEPH-DEVSTACK-BLOCK-POOL-TAIL-v1\x00not-json")
        assert _read_tail_marker(str(parent)) is None
