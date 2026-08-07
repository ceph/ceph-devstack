import pytest

from ceph_devstack import config, parse_args
from ceph_devstack.resources.ceph import CephDevStack


class TestCephDevStackInit:
    def test_init_sets_active_services(self):
        devstack = CephDevStack("teuthology")
        assert "postgres" in devstack.active_services
        assert "teuthology" in devstack.active_services

    def test_init_defaults_to_config_stack(self):
        devstack = CephDevStack()
        assert devstack.stack_name == "teuthology"

    def test_init_unknown_stack_raises(self):
        with pytest.raises(ValueError, match="Unknown stack"):
            CephDevStack("nonexistent")

    def test_init_merges_container_overrides(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[stacks.minimal]
services = ["teuthology", "testnode"]

[stacks.minimal.containers.testnode]
count = 1
"""
        )
        config.load(config_file)
        devstack = CephDevStack("minimal")
        assert config["containers"]["testnode"]["count"] == 1
        assert devstack.active_services == ["teuthology", "testnode"]


class TestStackDataDir:
    def test_data_dir_reflects_stack(self):
        build = CephDevStack("build-ceph")
        ceph = CephDevStack("ceph")
        assert str(build.data_dir).endswith("/build-ceph")
        assert str(ceph.data_dir).endswith("/ceph")

    def test_data_dir_does_not_mutate_base_config(self):
        base = config["data_dir"]
        CephDevStack("ceph")
        assert config["data_dir"] == base


class TestParseArgsStack:
    def test_parse_args_stack(self):
        args = parse_args(["--stack", "ceph", "start"])
        assert args.stack == "ceph"
        assert args.command == "start"

    def test_parse_args_stack_default_none(self):
        args = parse_args(["start"])
        assert args.stack is None
