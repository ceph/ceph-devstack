import pytest

from ceph_devstack import config, parse_args


class TestApplyStack:
    def test_apply_stack_sets_active_stack(self):
        config.apply_stack("teuthology")
        assert config.active_stack == "teuthology"

    def test_apply_stack_sets_active_services(self):
        config.apply_stack("teuthology")
        assert "postgres" in config.active_services
        assert "teuthology" in config.active_services

    def test_apply_stack_uses_config_default(self):
        config.apply_stack()
        assert config.active_stack == "teuthology"

    def test_apply_stack_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown stack"):
            config.apply_stack("nonexistent")

    def test_apply_stack_merges_container_overrides(self, tmp_path):
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
        config.apply_stack("minimal")
        assert config["containers"]["testnode"]["count"] == 1
        assert config.active_services == ["teuthology", "testnode"]

    def test_apply_stack_merges_data_dir_override(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[stacks.ceph]
services = ["ceph_node"]
data_dir = "~/.local/share/ceph-devstack/custom-ceph"
"""
        )
        config.load(config_file)
        config.apply_stack("ceph")
        assert config["data_dir"] == "~/.local/share/ceph-devstack/custom-ceph"


class TestParseArgsStack:
    def test_parse_args_stack(self):
        args = parse_args(["--stack", "ceph", "start"])
        assert args.stack == "ceph"
        assert args.command == "start"

    def test_parse_args_stack_default_none(self):
        args = parse_args(["start"])
        assert args.stack is None
