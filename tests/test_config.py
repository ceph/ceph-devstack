import pytest
import tomlkit

from ceph_devstack import config, Config


@pytest.fixture(scope="function")
def test_config(tmp_path) -> Config:
    test_config = Config()
    config_file = tmp_path / "test_config.toml"
    config_file.write_text("")
    test_config.load(config_file)
    return test_config


class TestConfigDump:
    def test_config_dump_returns_string(self):
        result = config.dump()
        assert isinstance(result, str)

    def test_config_dump_is_valid_toml(self):
        result = config.dump()
        parsed = tomlkit.parse(result)
        assert isinstance(parsed, tomlkit.TOMLDocument)

    def test_config_contents_basic(self):
        result = config.dump()
        requires = [
            "containers",
            "data_dir",
            "postgres",
            "beanstalk",
            "paddles",
            "pulpito",
            "testnode",
            "teuthology",
            "archive",
        ]
        for item in requires:
            assert item in result


class TestConfigGetValue:
    def test_get_value_simple_key(self):
        result = config.get_value("data_dir")
        assert isinstance(result, str)

    def test_get_value_nested_count(self):
        result = config.get_value("containers.testnode.count")
        assert result == "3"

    def test_get_value_nested_loop_device_size(self):
        result = config.get_value("containers.testnode.loop_device_size")
        assert result == "5G"

    def test_get_value_nested_image(self):
        result = config.get_value("containers.testnode.image")
        assert "quay.io/ceph-infra/teuthology-testnode:main" in result

    def test_get_value_returns_string_for_int(self):
        result = config.get_value("containers.testnode.count")
        assert result == "3"
        assert isinstance(result, str)


class TestConfigSet:
    def test_set_value_simple_key(self, test_config):
        test_config.set_value("test_key", "test_value")
        assert test_config["test_key"] == "test_value"

    def test_set_value_returns_value(self, test_config):
        assert test_config.set_value("test_key", "test_value") == "test_value"

    def test_set_value_nested_key(self, test_config):
        test_config.set_value("test_section.test_key", "test_value")
        assert test_config["test_section"]["test_key"] == "test_value"

    def test_set_value_updates_user_obj(self, test_config):
        test_config.set_value("new_key", "new_value")
        assert "new_key" in test_config.user_obj

    def test_set_value_creates_intermediate_sections(self, test_config):
        test_config.set_value("deep.nested.key", "value")
        assert test_config.user_obj["deep"]["nested"]["key"] == "value"

    def test_set_value_overrides_existing(self, test_config):
        original_count = test_config["containers"]["testnode"]["count"]
        new_count = original_count + 2
        test_config.set_value("containers.testnode.count", str(new_count))
        assert test_config["containers"]["testnode"]["count"] != original_count
        assert test_config["containers"]["testnode"]["count"] == new_count


class TestConfigUnset:
    def test_unset_value_simple_key(self, test_config):
        test_config.set_value("test_key", "test_value")
        assert "test_key" in test_config
        assert test_config["test_key"] == "test_value"
        test_config.unset_value("test_key")
        assert "test_key" not in test_config


class TestConfigDefaults:
    def test_config_defaults(self):
        assert config == {
            "stack": "teuthology",
            "data_dir": "~/.local/share/ceph-devstack",
            "block_pool": {
                "state_dir": "~/.local/share/ceph-devstack",
            },
            "stacks": {
                "teuthology": {
                    "services": [
                        "postgres",
                        "paddles",
                        "beanstalk",
                        "pulpito",
                        "teuthology",
                        "testnode",
                        "archive",
                    ],
                    "secrets": ["ssh_keypair"],
                },
                "ceph": {
                    "services": ["ceph_node"],
                    "secrets": [],
                    "data_dir": "~/.local/share/ceph-devstack/ceph",
                },
            },
            "containers": {
                "archive": {"image": "python:alpine"},
                "beanstalk": {"image": "quay.io/ceph-infra/teuthology-beanstalkd:main"},
                "paddles": {"image": "quay.io/ceph-infra/paddles:main"},
                "postgres": {
                    "image": "quay.io/ceph-infra/teuthology-postgresql:latest"
                },
                "pulpito": {"image": "quay.io/ceph-infra/pulpito:main"},
                "testnode": {
                    "count": 3,
                    "loop_device_count": 1,
                    "loop_device_size": "5G",
                    "image": "quay.io/ceph-infra/teuthology-testnode:main",
                },
                "teuthology": {"image": "quay.io/ceph-infra/teuthology-dev:main"},
                "ceph_node": {
                    "image": "quay.io/ceph-ci/ceph:main",
                    "loop_device_count": 3,
                    "loop_device_size": "5G",
                    "dashboard_port": 8080,
                    "dashboard_ssl": False,
                },
            },
        }
