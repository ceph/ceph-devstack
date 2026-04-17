import asyncio
from asyncio.subprocess import SubprocessStreamProtocol
import functools
import os
import pathlib
import subprocess

from typing import Dict, List, Optional

from ceph_devstack import logger, VERBOSE


class LoggingStreamProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    def __init__(self, limit, loop, log_level):
        self.log_level = log_level
        super().__init__(limit=limit, loop=loop)

    def pipe_data_received(self, fd, data):
        logger.log(
            self.log_level,
            (data.decode() if isinstance(data, bytes) else str(data)).rstrip("\n"),
        )
        super().pipe_data_received(fd, data)


class Command:
    def __init__(
        self,
        args: List[str],
        cwd: Optional[pathlib.Path] = None,
        env: Optional[Dict] = None,
        stream_output: bool = False,
    ):
        self.args = args
        self.env = os.environ | (env or {})
        self.kwargs: Dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
        }
        if cwd:
            self.kwargs.update(cwd=cwd)
        self.stream_output = stream_output

    def _make_log_msg(self) -> str:
        msg = "> " + " ".join(self.args)
        if (cwd := str(self.kwargs.get("cwd", "."))) != ".":
            msg = f"{msg} cwd='{cwd}'"
        return msg

    def run(self) -> subprocess.Popen:
        logger.log(VERBOSE, self._make_log_msg())
        proc = subprocess.Popen(
            args=self.args,
            env=self.env,
            **self.kwargs,
        )
        proc.wait()
        return proc

    async def arun(self) -> asyncio.subprocess.Process:
        logger.log(VERBOSE, self._make_log_msg())
        loop = asyncio.get_running_loop()
        protocol_factory: (
            functools.partial[SubprocessStreamProtocol]
            | functools.partial[LoggingStreamProtocol]
        )
        if self.stream_output:
            protocol_factory = functools.partial(
                LoggingStreamProtocol,
                limit=2**16,
                loop=loop,
                log_level=VERBOSE,
            )
        else:
            protocol_factory = functools.partial(
                asyncio.subprocess.SubprocessStreamProtocol,
                limit=2**16,
                loop=loop,
            )
        transport, protocol = await loop.subprocess_exec(
            protocol_factory,
            *self.args,
            env=self.env,
            **self.kwargs,
        )
        return asyncio.subprocess.Process(
            transport,
            protocol,
            loop,
        )

    def __str__(self):
        return " ".join(self.args)
