from ceph_devstack.resources.ceph.host_loops import (
    allocate_loop_numbers,
    discover_used_loop_numbers,
    host_loop_path,
    owner_loop_numbers,
)


class TestHostLoops:
    def test_host_loop_path(self):
        assert host_loop_path(21) == "/dev/loop21"

    def test_discover_used_loop_numbers_from_backing_files(self, tmp_path):
        (tmp_path / "testnode_0-1").write_bytes(b"")
        (tmp_path / "ceph_node-4").write_bytes(b"")
        (tmp_path / "ignore-me").write_bytes(b"")
        assert discover_used_loop_numbers(tmp_path) == {1, 4}

    def test_owner_loop_numbers(self, tmp_path):
        (tmp_path / "testnode_1-2").write_bytes(b"")
        (tmp_path / "testnode_1-0").write_bytes(b"")
        assert owner_loop_numbers("testnode_1", tmp_path) == [0, 2]

    def test_allocate_loop_numbers_uses_lowest_free(self, tmp_path):
        (tmp_path / "other-0").write_bytes(b"")
        assert allocate_loop_numbers("testnode_0", 2, tmp_path) == [1, 2]

    def test_allocate_loop_numbers_reuses_owner_backing_files(self, tmp_path):
        (tmp_path / "testnode_1-4").write_bytes(b"")
        (tmp_path / "testnode_1-1").write_bytes(b"")
        (tmp_path / "other-0").write_bytes(b"")
        assert allocate_loop_numbers("testnode_1", 3, tmp_path) == [1, 4, 2]
