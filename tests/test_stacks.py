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
        assert devstack.containers_config["testnode"]["count"] == 1
        assert config["containers"]["testnode"]["count"] == 3
        assert len(devstack.service_specs["testnode"]["objects"]) == 1
        teuthology = devstack.service_specs["teuthology"]["objects"][0]
        assert "testnode_count=1" in teuthology.create_cmd
        assert devstack.active_services == ["teuthology", "testnode"]


class TestStackDataDir:
    def test_data_dir_reflects_stack(self):
        devstack = CephDevStack("teuthology")
        assert str(devstack.data_dir).endswith("/teuthology")


class TestParseArgsStack:
    def test_parse_args_stack(self):
        args = parse_args(["--stack", "ceph", "start"])
        assert args.stack == "ceph"
        assert args.command == "start"

    def test_parse_args_stack_default_none(self):
        args = parse_args(["start"])
        assert args.stack is None
