import asyncio
from asyncio.subprocess import SubprocessStreamProtocol
import contextlib
import functools
import os
import pathlib
import psutil
import signal
import subprocess

from typing import Dict, List, Optional, Tuple

from ceph_devstack import logger, VERBOSE

_TERMINATE_TIMEOUT = 3.0
_KILL_TIMEOUT = 1.0


class Subprocess(asyncio.subprocess.Process):
    async def _close_transport(self) -> None:
        transport = getattr(self, "_transport", None)
        if transport is not None and not transport.is_closing():
            transport.close()

    async def _wait_for_exit(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(super().wait()), timeout=timeout)

    async def _terminate(self) -> None:
        if self.returncode is not None:
            await self._close_transport()
            return
        self.signal_children(signal.SIGTERM, recursive=True)
        with contextlib.suppress(ProcessLookupError):
            self.kill()
        await self._wait_for_exit(_TERMINATE_TIMEOUT)
        if self.returncode is None:
            self.signal_children(signal.SIGKILL, recursive=True)
        with contextlib.suppress(ProcessLookupError):
            self.kill()
        await self._wait_for_exit(_KILL_TIMEOUT)
        await self._close_transport()

    async def wait(self) -> int:
        try:
            return await super().wait()
        except asyncio.CancelledError:
            await self._terminate()
            raise

    async def communicate(self, input=None):
        try:
            return await super().communicate(input)
        except asyncio.CancelledError:
            await self._terminate()
            raise

    def child_pids(self, recursive=True):
        if self.pid is None:
            return []
        return [
            child.pid
            for child in psutil.Process(self.pid).children(recursive=recursive)
        ]

    def signal_children(self, signal: signal.Signals, recursive=True):
        for pid in self.child_pids(recursive=recursive):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal)

    async def collect_output(self) -> Tuple[str, str]:
        stdout = b""
        stderr = b""
        if self.stdout is not None:
            stdout = await self.stdout.read()
        if self.stderr is not None:
            stderr = await self.stderr.read()
        return stdout.decode(), stderr.decode()

    async def log_failure(self, cmd: List[str]) -> Tuple[str, str]:
        stdout, stderr = await self.collect_output()
        returncode = self.returncode if self.returncode is not None else -1
        logger.error(f"Command failed ({returncode}): {' '.join(cmd)}")
        for line in stderr.rstrip("\n").splitlines():
            logger.error(line)
        for line in stdout.rstrip("\n").splitlines():
            logger.error(line)
        return stdout, stderr


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

    async def arun(self) -> Subprocess:
        logger.log(VERBOSE, self._make_log_msg())
        loop = asyncio.get_running_loop()
        kwargs = dict(self.kwargs)
        if self.stream_output:
            # Inherit stdout/stderr so long-running commands (e.g. dnf builddep)
            # are not blocked when their output exceeds the StreamReader limit.
            kwargs["stdout"] = None
            kwargs["stderr"] = None
        protocol_factory = functools.partial(
            SubprocessStreamProtocol,
            limit=2**16,
            loop=loop,
        )
        transport, protocol = await loop.subprocess_exec(
            protocol_factory,
            *self.args,
            env=self.env,
            start_new_session=True,
            **kwargs,
        )
        return Subprocess(transport, protocol, loop)

    def __str__(self):
        return " ".join(self.args)
