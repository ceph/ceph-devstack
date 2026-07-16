import pytest

from ceph_devstack.host import LocalHost


class TestLocalHost:
    @pytest.fixture
    def host(self):
        return LocalHost()

    def test_path_exists_expands_tilde(self, host):
        assert host.path_exists("~") is True
        assert host.path_exists("~/nonexistent_dir_12345") is False
