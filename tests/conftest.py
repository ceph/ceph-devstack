import pytest

from ceph_devstack import config


@pytest.fixture(autouse=True)
def reset_config():
    config.load()
    yield
