import io
import contextlib
import pathlib
import secrets
import string

from datetime import datetime, timedelta

import pytest

from ceph_devstack import config
from ceph_devstack.resources.ceph.utils import (
    get_logtimestamp,
    get_jobs,
)
from ceph_devstack.resources.ceph import CephDevStack


class TestDevStack:
    def test_get_logtimestamp(self):
        dirname = "root-2025-03-20_18:34:43-orch:cephadm:smoke-small-main-distro-default-testnode"
        assert get_logtimestamp(dirname) == datetime(2025, 3, 20, 18, 34, 43)

    def test_get_jobs_returns_job_on_unique_job(self, tmp_path):
        temp_dir_path = pathlib.Path(tmp_path)
        job_path = temp_dir_path / "97"
        job_path.mkdir()
        result = get_jobs(temp_dir_path)
        assert len(result) == 1
        assert result[0].name == "97"

    def test_get_jobs_throws_filenotfound_on_missing_job(self):
        with pytest.raises(FileNotFoundError):
            get_jobs(pathlib.Path("/fake/path"))

    async def test_logs_command_display_log_file_of_latest_run(
        self, tmp_path, create_log_file
    ):
        config["data_dir"] = str(tmp_path)
        f = io.StringIO()
        content = "custom log content"

        create_log_file(
            tmp_path,
            timestamp=datetime.now() - timedelta(days=40),
        )
        create_log_file(tmp_path, timestamp=datetime.now(), content=content)

        with contextlib.redirect_stdout(f):
            devstack = CephDevStack()
            await devstack.logs()
        assert content in f.getvalue()

    async def test_logs_display_roughly_contents_of_log_file(
        self, tmp_path, create_log_file
    ):
        config["data_dir"] = str(tmp_path)
        f = io.StringIO()
        content = "".join(
            secrets.choice(string.ascii_letters + string.digits)
            for _ in range(6 * 8 * 1024)
        )
        create_log_file(
            tmp_path,
            timestamp=datetime.now(),
            content=content,
        )

        with contextlib.redirect_stdout(f):
            devstack = CephDevStack()
            await devstack.logs()
        assert content == f.getvalue()

    async def test_logs_command_display_log_file_of_given_job_id(
        self, tmp_path, create_log_file
    ):
        config["data_dir"] = str(tmp_path)
        f = io.StringIO()
        content = "custom log message"
        now = datetime.now()

        create_log_file(
            tmp_path,
            timestamp=now,
            test_type="ceph",
            job_id="1",
            content="another log",
        )
        create_log_file(
            tmp_path,
            timestamp=now,
            test_type="ceph",
            job_id="2",
            content=content,
        )

        with contextlib.redirect_stdout(f):
            devstack = CephDevStack()
            await devstack.logs(job_id="2")
        assert content in f.getvalue()

    async def test_logs_display_content_of_provided_run_name(
        self, tmp_path, create_log_file
    ):
        config["data_dir"] = str(tmp_path)
        f = io.StringIO()
        content = "custom content"
        create_log_file(
            tmp_path,
            timestamp=datetime.now(),
        )
        run_name: pathlib.Path = create_log_file(
            tmp_path,
            timestamp=datetime.now() - timedelta(days=3),
            content=content,
        ).parent.parent

        with contextlib.redirect_stdout(f):
            devstack = CephDevStack()
            await devstack.logs(run_name=run_name)
        assert content in f.getvalue()

    async def test_logs_locate_display_file_path_instead_of_config(
        self, tmp_path, create_log_file
    ):
        config["data_dir"] = str(tmp_path)
        f = io.StringIO()
        log_file = create_log_file(tmp_path)
        with contextlib.redirect_stdout(f):
            devstack = CephDevStack()
            await devstack.logs(locate=True)
        assert str(log_file) in f.getvalue()
