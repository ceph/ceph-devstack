import asyncio
import json
import os

from typing import Dict, List, Optional

from ceph_devstack import config, logger
from ceph_devstack.resources import PodmanResource


class Container(PodmanResource):
    network: str
    secret: List[str]
    cmd_vars: List[str] = ["name", "image", "image_name", "image_tag"]
    build_cmd: List[str] = [
        "podman",
        "build",
        "-t",
        "{image_name}:{image_tag}",
        ".",
    ]
    create_cmd: List[str] = ["podman", "container", "create", "{name}"]
    remove_cmd: List[str] = ["podman", "container", "rm", "-f", "{name}"]
    start_cmd: List[str] = ["podman", "container", "start", "{name}"]
    stop_cmd: List[str] = ["podman", "container", "stop", "{name}"]
    exists_cmd: List[str] = ["podman", "container", "inspect", "{name}"]
    pull_cmd: List[str] = ["podman", "pull", "{image}"]
    wait_cmd: List[str] = ["podman", "wait", "{name}"]
    env_vars: Dict[str, Optional[str]] = {}
    _image_name: str | None = None

    def __init__(self, name: str = ""):
        super().__init__(name)
        self.env_vars = {**self.__class__.env_vars}
        for key in self.env_vars:
            if os.environ.get(key):
                self.env_vars[key] = os.environ[key]

    def add_env_to_args(self, args: List):
        args = super().format_cmd(args)
        for key, value in self.env_vars.items():
            if not value:
                continue
            args.insert(-1, "-e")
            args.insert(-1, f"{key}={value}")
        return args

    @property
    def config(self):
        return config["containers"].get(self.__class__.__name__.lower(), {})

    @property
    def image_name(self) -> str:
        if self._image_name is not None:
            return self._image_name
        return self.__class__.__name__.lower()

    @property
    def image(self):
        if self.repo:
            return f"localhost/{self.image_name}"
        return self.config["image"]

    @property
    def image_tag(self):
        if ":" not in self.image:
            return "latest"
        return self.image.split(":")[-1]

    @property
    def repo(self):
        repo = self.config.get("repo", "")
        try:
            return repo.expanduser()
        except AttributeError:
            return os.path.expanduser(repo)

    @property
    def cwd(self):
        return self.repo or "."

    async def pull(self):
        if not getattr(self, "pull_cmd", None):
            return
        if self.image.startswith("localhost/"):
            return
        logger.debug(f"{self.name}: pulling from: {self.image}")
        await self.cmd(
            self.format_cmd(self.pull_cmd),
            check=True,
            stream_output=True,
        )

    async def build(self):
        if not getattr(self, "repo", None):
            return
        logger.debug(f"{self.name}: building from repo: {self.repo}")
        await self.cmd(
            self.format_cmd(self.build_cmd),
            check=True,
            stream_output=True,
        )
        logger.debug(f"{self.name}: built")

    async def create(self):
        if not getattr(self, "create_cmd", None):
            return
        if await self.exists():
            return
        args = self.add_env_to_args(self.format_cmd(self.create_cmd))
        logger.debug(f"{self.name}: creating")
        await self.cmd(
            args,
            check=True,
            stream_output=True,
        )
        logger.debug(f"{self.name}: created")

    async def start(self):
        if not getattr(self, "start_cmd", None):
            return
        logger.debug(f"{self.name}: starting")
        await self.cmd(
            self.format_cmd(self.start_cmd),
            check=True,
            stream_output=True,
        )
        if "--health-cmd" in self.create_cmd or "--healthcheck-cmd" in self.create_cmd:
            rc = None
            while rc != 0:
                proc = await self.cmd(
                    self.format_cmd(["podman", "healthcheck", "run", "{name}"]),
                )
                rc = await proc.wait()
                await asyncio.sleep(1)
        logger.debug(f"{self.name}: started")

    async def stop(self):
        if not getattr(self, "stop_cmd", None):
            return
        logger.debug(f"{self.name}: stopping")
        await self.cmd(
            self.format_cmd(self.stop_cmd),
            stream_output=True,
        )
        logger.debug(f"{self.name}: stopping")

    async def remove(self):
        if not getattr(self, "remove_cmd", None):
            return
        logger.debug(f"{self.name}: removing")
        await super().remove()
        logger.debug(f"{self.name}: removed")

    async def is_running(self):
        proc = await self.cmd(self.format_cmd(self.exists_cmd))
        assert proc.stdout is not None
        if not await self.exists():
            return False
        result = json.loads(await proc.stdout.read())
        if not result:
            return False
        return result[0]["State"]["Status"].lower() == "running"

    async def wait(self) -> Optional[int]:
        proc = await self.cmd(self.format_cmd(self.wait_cmd))
        out, err = await proc.communicate()
        if proc.returncode:
            logger.error(f"Could not wait for {self.name}: {err.decode().strip()}")
            return proc.returncode
        return int(out.decode().strip())
