import os
import pathlib
import pytest
import random

from datetime import datetime, timedelta

from ceph_devstack import config


@pytest.fixture(autouse=True)
def reset_config():
    config.load()
    yield


@pytest.fixture(scope="function")
def create_log_file():
    def _create_log_file(data_dir: pathlib.Path, **kwargs) -> pathlib.Path:
        parts = {
            "timestamp": (datetime.now() - timedelta(days=random.randint(1, 100))),
            "test_type": random.choice(["ceph", "rgw", "rbd", "mds"]),
            "job_id": random.randint(1, 100),
            "content": "some log data",
            **kwargs,
        }
        timestamp = parts["timestamp"].strftime("%Y-%m-%d_%H:%M:%S")
        test_type = parts["test_type"]
        job_id = parts["job_id"]
        content = parts["content"]

        run_name = f"root-{timestamp}-orch:cephadm:{test_type}-small-main-distro-default-testnode"
        log_dir = data_dir / "archive" / run_name / str(job_id)

        os.makedirs(log_dir, exist_ok=True)
        time_ = parts["timestamp"].timestamp()
        os.utime(log_dir, times=(time_, time_))
        log_file = log_dir / "teuthology.log"
        log_file.write_text(content)
        os.utime(log_file, times=(time_, time_))
        return log_file

    return _create_log_file
