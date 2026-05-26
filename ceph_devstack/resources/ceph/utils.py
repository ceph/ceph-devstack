import re
from datetime import datetime

from ceph_devstack.resources.ceph.exceptions import TooManyJobsFound

RUN_DIRNAME_PATTERN = re.compile(
    r"^(?P<username>^[a-z_]([a-z0-9_-]{0,31}|[a-z0-9_-]{0,30}))-(?P<timestamp>\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})"
)


def get_logtimestamp(dirname: str) -> datetime:
    match_ = RUN_DIRNAME_PATTERN.search(dirname)
    assert match_
    return datetime.strptime(match_.group("timestamp"), "%Y-%m-%d_%H:%M:%S")


def get_most_recent_run(runs: list[str]) -> str:
    try:
        run_name = next(
            iter(
                sorted(
                    (
                        dirname
                        for dirname in runs
                        if RUN_DIRNAME_PATTERN.search(dirname)
                    ),
                    key=lambda dirname: get_logtimestamp(dirname),
                    reverse=True,
                )
            )
        )
        return run_name
    except StopIteration as e:
        raise FileNotFoundError from e


def get_job_id(jobs: list[str]):
    job_dir_pattern = re.compile(r"^\d+$")
    dirs = [d for d in jobs if job_dir_pattern.match(d)]

    if len(dirs) == 0:
        raise FileNotFoundError
    elif len(dirs) > 1:
        raise TooManyJobsFound(dirs)
    return dirs[0]
