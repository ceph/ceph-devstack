import asyncio
import logging
import os
import pathlib
import socket
import sys
import yaml

from packaging.version import parse as parse_version, Version
from typing import Dict, List, Optional, Union

from .exec import Command

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
    ) -> asyncio.subprocess.Process:
        return await self.cmd(
            args, cwd=cwd, env=env, stream_output=stream_output
        ).arun()

    def path_exists(self, path: Union[str, pathlib.Path]):
        if isinstance(path, pathlib.Path):
            return path.exists()
        return os.path.exists(path)

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

    async def podman_info(self, force: bool = False) -> Dict:
        if force or not hasattr(self, "_podman_info"):
            proc = await self.arun(["podman", "info"])
            assert proc.stdout is not None
            await proc.wait()
            stdout = await proc.stdout.read()
            self._podman_info = yaml.safe_load(stdout.decode().strip())
        return self._podman_info

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


class LocalHost(Host):
    pass


class RemoteHost(Host):
    type = "remote"
    base_args = ["podman", "machine", "ssh", "--"]

    def cmd(
        self,
        args: List[str],
        cwd: Optional[pathlib.Path] = None,
        env: Optional[Dict] = None,
        stream_output: bool = False,
    ):
        if args[0] != "podman":
            args = self.base_args + args
        return super().cmd(args, cwd=cwd, env=env, stream_output=stream_output)

    def path_exists(self, path: Union[str, pathlib.Path]):
        path = os.path.expanduser(path)
        proc = host.run(["ls", path])
        return proc.returncode == 0

    def hostname(self) -> str:
        proc = self.run(["hostname"])
        assert proc.stdout is not None
        return proc.stdout.read().decode().strip()


local_host = LocalHost()

if sys.platform == "darwin":
    host = RemoteHost()
else:
    host = local_host
