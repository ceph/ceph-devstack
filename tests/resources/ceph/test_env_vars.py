import pytest

from ceph_devstack.resources.ceph import containers


ANY_VALUE = "ANY_VALUE"


class _TestContainerEnvVars:
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        raise NotImplementedError

    @pytest.fixture(scope="class")
    @classmethod
    def env_vars(self):
        # return {}
        raise NotImplementedError

    def test_env_vars(self, cls, env_vars):
        obj = cls()
        if env_vars == {}:
            assert obj.env_vars == env_vars
        else:
            for env_var, value in env_vars.items():
                assert env_var in obj.env_vars
                assert obj.env_vars[env_var] == value


class TestPostgres(_TestContainerEnvVars):
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        return containers.Postgres

    @pytest.fixture(scope="class")
    @classmethod
    def env_vars(self):
        return {
            "POSTGRES_USER": "root",
            "POSTGRES_PASSWORD": "password",
            "APP_DB_USER": "admin",
            "APP_DB_PASS": "password",
            "APP_DB_NAME": "paddles",
        }


class TestPaddles(_TestContainerEnvVars):
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        return containers.Paddles

    @pytest.fixture(scope="class")
    @classmethod
    def env_vars(self):
        return {
            "PADDLES_SERVER_HOST": "0.0.0.0",
        }


class TestPulpito(_TestContainerEnvVars):
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        return containers.Pulpito

    @pytest.fixture(scope="class")
    @classmethod
    def env_vars(self):
        return {
            "PULPITO_PADDLES_ADDRESS": "http://paddles:8080",
        }


class TestTestNode(_TestContainerEnvVars):
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        return containers.TestNode

    @pytest.fixture(scope="class")
    @classmethod
    def env_vars(self):
        return {
            "CEPH_VOLUME_ALLOW_LOOP_DEVICES": "true",
        }


class TestTeuthology(_TestContainerEnvVars):
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        return containers.Teuthology

    @pytest.fixture(scope="class")
    @classmethod
    def env_vars(self):
        return {
            "SSH_PRIVKEY": "",
            "SSH_PRIVKEY_FILE": "",
            "TEUTHOLOGY_MACHINE_TYPE": "",
            "TEUTHOLOGY_TESTNODES": "",
            "TEUTHOLOGY_BRANCH": "",
            "TEUTHOLOGY_CEPH_BRANCH": "",
            "TEUTHOLOGY_CEPH_REPO": "",
            "TEUTHOLOGY_SUITE": "",
            "TEUTHOLOGY_SUITE_BRANCH": "",
            "TEUTHOLOGY_SUITE_REPO": "",
            "TEUTHOLOGY_SUITE_EXTRA_ARGS": "",
        }


class TestBeanstalk(_TestContainerEnvVars):
    @pytest.fixture(scope="class")
    @classmethod
    def cls(self):
        return containers.Beanstalk

    @pytest.fixture(scope="class")
    @classmethod
    def env_vars(self):
        return {}
