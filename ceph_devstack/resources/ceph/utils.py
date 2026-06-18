import pathlib
import re
from datetime import datetime
from typing import List

from ceph_devstack.resources.ceph.exceptions import TooManyJobsFound

RUN_DIRNAME_PATTERN = re.compile(
    r"^(?P<username>^[a-z_]([a-z0-9_-]{0,31}|[a-z0-9_-]{0,30}))-(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})"
)


def get_logtimestamp(dirname: str) -> datetime:
    match_ = RUN_DIRNAME_PATTERN.search(dirname)
    assert match_
    return datetime.strptime(match_.group("timestamp"), "%Y-%m-%d_%H:%M:%S")


def get_runs(directory: pathlib.Path) -> List[pathlib.Path]:
    return sorted(
        (
            dir_
            for dir_ in directory.expanduser().absolute().iterdir()
            if RUN_DIRNAME_PATTERN.search(dir_.name)
        ),
        key=lambda dir_: dir_.stat().st_mtime,
        reverse=True,
    )


def get_job_id(jobs: list[str]):
    job_dir_pattern = re.compile(r"^\d+$")
    dirs = [d for d in jobs if job_dir_pattern.match(d)]

    if len(dirs) == 0:
        raise FileNotFoundError
    elif len(dirs) > 1:
        raise TooManyJobsFound(dirs)
    return dirs[0]
