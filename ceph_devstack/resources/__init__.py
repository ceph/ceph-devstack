#!/usr/bin/env python3
import argparse
import json
import os
import subprocess

from pathlib import Path
from subprocess import CalledProcessError
from typing import List, Dict, Set

from ceph_devstack.exec import Subprocess
from ceph_devstack.host import host, local_host


class DevStack:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.env: Dict[str, str] = {}

    def choose_teuthology_branch(self):
        branch = os.environ.get("TEUTHOLOGY_BRANCH", self.get_current_branch())
        self.env["TEUTH_BRANCH"] = branch
        return branch

    def get_current_branch(self, repo_path: str = "."):
        return subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=repo_path,
        ).decode()


class PodmanResource:
    cwd = "."
    exists_cmd: List[str] = []
    create_cmd: List[str] = []
    remove_cmd: List[str] = []
    start_cmd: List[str] = []
    stop_cmd: List[str] = []
    cmd_vars: List[str] = ["name"]
    log: Dict[str, Set[str]] = {}
    _name: str | None = None

    def __init__(self, name: str = ""):
        if name:
            self._name = name

    @property
    def name(self) -> str:
        if self._name is not None:
            return self._name
        return self.__class__.__name__.lower()

    async def cmd(
        self,
        args: List[str],
        check: bool = False,
        force_local: bool = False,
        stream_output: bool = False,
    ) -> Subprocess:
        exec_host = local_host if force_local else host
        proc = await exec_host.arun(
            args,
            cwd=Path(self.cwd),
            stream_output=stream_output,
        )
        returncode = await proc.wait()
        if check and returncode != 0:
            stdout, stderr = await proc.log_failure(args)
            raise CalledProcessError(
                returncode,
                args,
                output=stdout or None,
                stderr=stderr or None,
            )
        return proc

    def format_cmd(self, args: List):
        vars = {}
        for k in self.cmd_vars:
            v = getattr(self, k, None)
            if v is not None:
                if isinstance(v, Path):
                    v = v.expanduser()
                vars[k] = v
        return [s.format(**vars) for s in args]

    async def apply(self, action: str):
        method = getattr(self, action, None)
        if method is None:
            return
        await method()

    async def inspect(self):
        proc = await self.cmd(self.format_cmd(self.exists_cmd))
        out, err = await proc.communicate()
        return json.loads(out)
        return json.loads(proc.stdout.read())
        if proc.stdout is None:
            return {}
        return json.loads((await proc.stdout.read()).decode())

    async def exists(self):
        if not self.exists_cmd:
            return False
        proc = await self.cmd(self.format_cmd(self.exists_cmd), check=False)
        return await proc.wait() == 0

    async def create(self):
        if not await self.exists():
            await self.cmd(self.format_cmd(self.create_cmd), check=True)

    async def remove(self):
        await self.cmd(self.format_cmd(self.remove_cmd))

    def __repr__(self):
        param_str = "" if self._name is None else f'name="{self._name}"'
        return f"{self.__class__.__name__}({param_str})"
