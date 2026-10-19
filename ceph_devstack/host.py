import functools
import json
import logging
import math
import os
import pathlib
import shlex
import socket
import subprocess
import sys
import yaml

from packaging.version import parse as parse_version, Version
from typing import Dict, List, Optional, Tuple, Union

from .exec import Command, Subprocess

logger = logging.getLogger(__name__)


class Host:
    type = "local"

    def cmd(
        self,
        args: List[str],
        cwd: Optional[pathlib.Path] = None,
        env: Optional[Dict] = None,
        stream_output: bool = False,
    ) -> Command:
        return Command(
            args,
            cwd=cwd,
            env=env,
            stream_output=stream_output,
        )

    def run(
        self,
        args: List[str],
        cwd: Optional[pathlib.Path] = None,
        env: Optional[Dict] = None,
    ):
        return self.cmd(args, cwd=cwd, env=env).run()

    async def arun(
        self,
        args: List[str],
        cwd: Optional[pathlib.Path] = None,
        env: Optional[Dict] = None,
        stream_output: bool = False,
    ) -> Subprocess:
        return await self.cmd(
            args, cwd=cwd, env=env, stream_output=stream_output
        ).arun()

    def path_exists(self, path: Union[str, pathlib.Path]):
        if isinstance(path, str):
            path = pathlib.Path(path)
        return path.expanduser().exists()

    def output(
        self, args: List[str], input: Optional[bytes] = None
    ) -> subprocess.CompletedProcess:
        """Run args to completion on this host; capture stdout/stderr as bytes.

        This blocks the caller (and any running event loop) until the command
        exits; it is meant for short queries and small device reads/writes.
        """
        return self.cmd(args).capture(input)

    def read_text(self, path: Union[str, pathlib.Path]) -> Optional[str]:
        """Contents of a file on this host, or None if it cannot be read."""
        proc = self.output(["cat", str(path)])
        return proc.stdout.decode() if proc.returncode == 0 else None

    def list_dir(self, path: Union[str, pathlib.Path]) -> List[str]:
        """Entry names in a directory on this host; empty if it is unreadable."""
        proc = self.output(["ls", "-1", str(path)])
        return proc.stdout.decode().splitlines() if proc.returncode == 0 else []

    def realpath(self, path: Union[str, pathlib.Path]) -> str:
        """Canonical path on this host; the input unchanged if it can't resolve."""
        proc = self.output(["realpath", str(path)])
        return proc.stdout.decode().strip() if proc.returncode == 0 else str(path)

    def device_mounted(self, device: str) -> bool:
        """True if a filesystem on this host is mounted from device."""
        proc = self.output(["findmnt", "-n", "-o", "TARGET", "--source", device])
        return proc.returncode == 0 and bool(proc.stdout.strip())

    def read_bytes(
        self, path: Union[str, pathlib.Path], offset: int, length: int
    ) -> bytes:
        """Read length (> 0) bytes at offset from a file or device on this host."""
        bs = math.gcd(offset, length)
        args = ["dd", f"if={path}", f"bs={bs}", f"skip={offset // bs}"]
        proc = self.output(args + [f"count={length // bs}"])
        if proc.returncode != 0:
            raise OSError(f"cannot read {path}: {proc.stderr.decode().strip()}")
        return proc.stdout

    def write_bytes(
        self, path: Union[str, pathlib.Path], offset: int, data: bytes
    ) -> None:
        """Write data (non-empty) at offset into a file or device on this host."""
        bs = math.gcd(offset, len(data))
        args = ["dd", f"of={path}", f"bs={bs}", f"seek={offset // bs}"]
        proc = self.output(args + ["conv=notrunc,fsync"], input=data)
        if proc.returncode != 0:
            raise OSError(f"cannot write {path}: {proc.stderr.decode().strip()}")

    def hostname(self) -> str:
        name = socket.getfqdn()
        try:
            socket.gethostbyname(name)
            return name
        except socket.gaierror:
            return "localhost"

    def kernel_version(self) -> Version:
        if not hasattr(self, "_kernel_version"):
            proc = self.run(["uname", "-r"])
            assert proc.stdout is not None
            assert proc.wait() == 0, "`uname -r` failed?!"
            raw_version = proc.stdout.read().decode().strip()
            self._kernel_version = parse_version(raw_version.split("-")[0])
        return self._kernel_version

    @property
    def os_type(self) -> str:
        if not hasattr(self, "_os_type"):
            proc = self.run(["uname"])
            assert proc.wait() == 0, "uname doesn't work?!"
            if (uname_str := proc.stdout.read().decode().strip().lower()) == "linux":
                proc = self.run(["bash", "-c", ". /etc/os-release && echo $ID"])
                assert proc.stdout is not None
                assert proc.wait() == 0, "is /etc/os-release missing?"
                self._os_type = proc.stdout.read().decode().strip().lower()
            else:
                self._os_type = uname_str
        return self._os_type

    def package_manager(self) -> str | None:
        if self.os_type in ["centos", "rhel", "alma", "rocky", "fedora"]:
            return "dnf"
        elif self.os_type in ["debian", "ubuntu"]:
            return "apt"
        elif self.os_type == "darwin":
            return "brew"
        raise RuntimeError("Can't determine package manager")

    async def podman_info(self, force: bool = False) -> Dict:
        if force or not hasattr(self, "_podman_info"):
            proc = await self.arun(["podman", "info"])
            assert proc.stdout is not None
            await proc.wait()
            stdout = await proc.stdout.read()
            self._podman_info = yaml.safe_load(stdout.decode().strip())
        return self._podman_info

    async def podman_machine_info(self) -> List[Dict]:
        proc = await self.arun(["podman", "machine", "list", "--format", "json"])
        assert proc.stdout is not None
        await proc.wait()
        stdout = await proc.stdout.read()
        return json.loads(stdout)

    async def selinux_enforcing(self) -> bool:
        proc = await host.arun(["cat", "/sys/fs/selinux/enforce"])
        assert proc.stdout is not None
        await proc.wait()
        out = (await proc.stdout.read()).decode()
        return proc.returncode == 0 and out == "1"

    async def check_selinux_bool(self, name: str):
        proc = await host.arun(["getsebool", name])
        assert proc.stdout is not None
        out = await proc.stdout.read()
        return out.decode().strip() == f"{name} --> on"

    async def get_sysctl_value(self, name: str) -> int:
        proc = await host.arun(["sysctl", "-b", name])
        assert proc.stdout is not None
        out = await proc.stdout.read()
        return int(out.decode().strip())

    async def apparmor_enabled(self) -> bool:
        try:
            proc = await host.arun(["aa-enabled", "-q"])
        except FileNotFoundError:
            return False
        return await proc.wait() == 0

    def user_ids(self) -> Tuple[int, int]:
        """Return (uid, gid) of the user running this host."""
        return (os.getuid(), os.getgid())


class LocalHost(Host):
    pass


class RemoteHost(Host):
    type = "remote"
    base_args = ["podman", "machine", "ssh"]

    def _remote_args(self, args: List[str], stream_output: bool) -> List[str]:
        # Quote each arg so compound arguments like "bash -c '<cmd>'" stay as one shell word
        remote = list(self.base_args)
        remote.append("--")
        remote.extend(shlex.quote(arg) for arg in args)
        return remote

    def cmd(
        self,
        args: List[str],
        cwd: Optional[pathlib.Path] = None,
        env: Optional[Dict] = None,
        stream_output: bool = False,
    ):
        if args[0] != "podman" or args[:2] == ["podman", "unshare"]:
            args = self._remote_args(args, stream_output)
        return super().cmd(args, cwd=cwd, env=env, stream_output=stream_output)

    def path_exists(self, path: Union[str, pathlib.Path]):
        path = os.path.expanduser(path)
        proc = host.run(["ls", path])
        return proc.returncode == 0

    def hostname(self) -> str:
        proc = self.run(["hostname"])
        assert proc.stdout is not None
        return proc.stdout.read().decode().strip()

    def user_ids(self) -> Tuple[int, int]:
        """Return (uid, gid) of the user running podman on the container host."""
        return self._user_ids

    @functools.cached_property
    def _user_ids(self) -> Tuple[int, int]:
        return self._id("-u"), self._id("-g")

    def _id(self, flag: str) -> int:
        out = self.output(["id", flag]).stdout.decode().strip()
        try:
            return int(out)
        except ValueError as e:
            raise RuntimeError(f"Failed to parse output of 'id {flag}': {out!r}") from e


local_host = LocalHost()

if sys.platform == "darwin":
    host = RemoteHost()
else:
    host = local_host
