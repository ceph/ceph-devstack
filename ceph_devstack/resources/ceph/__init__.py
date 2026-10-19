import asyncio
import contextlib
import os
import pathlib
import tempfile

from subprocess import CalledProcessError
from typing import Iterator, Type, TypeVar, cast

from ceph_devstack import base_data_dir, config, deep_merge, logger
from ceph_devstack.host import host
from ceph_devstack.resources import StackResource
from ceph_devstack.resources.misc import Secret, Network
from ceph_devstack.resources.ceph.containers import (
    Postgres,
    Beanstalk,
    Paddles,
    Pulpito,
    TestNode,
    Teuthology,
    Archive,
    PackageRepo,
    Registry,
)
from ceph_devstack.resources.ceph.ceph_builder import CephBuilder
from ceph_devstack.resources.ceph.ceph_node import CephNode
from ceph_devstack.resources.ceph.host_loops import LoopDeviceMixin
from ceph_devstack.resources.ceph.requirements import (
    HasSudo,
    LoopControlDeviceExists,
    LoopControlDeviceWriteable,
    BlockPoolDiskGroup,
    BlockPoolParentAccessible,
    SELinuxModule,
)
from ceph_devstack.resources.ceph.utils import get_runs, get_jobs


class SSHKeyPair(Secret):
    _name = "id_rsa"
    cmd_vars = ["name", "privkey_path", "pubkey_path"]
    privkey_path = "id_rsa"
    pubkey_path = "id_rsa.pub"
    exists_cmds = [
        ["podman", "secret", "inspect", "{name}"],
        ["podman", "secret", "inspect", "{name}.pub"],
    ]
    create_cmds = [
        ["podman", "secret", "create", "{name}", "{privkey_path}"],
        ["podman", "secret", "create", "{name}.pub", "{pubkey_path}"],
    ]
    remove_cmds = [
        ["podman", "secret", "rm", "{name}"],
        ["podman", "secret", "rm", "{name}.pub"],
    ]

    async def exists(self):
        for exists_cmd in self.exists_cmds:
            proc = await self.cmd(self.format_cmd(exists_cmd), check=False)
            if await proc.wait():
                return False
        return True

    async def create(self):
        if await self.exists():
            return
        await self._get_ssh_keys()
        for create_cmd in self.create_cmds:
            await self.cmd(self.format_cmd(create_cmd), check=True)

    async def remove(self):
        for remove_cmd in self.remove_cmds:
            await self.cmd(self.format_cmd(remove_cmd))

    async def _get_ssh_keys(self):
        privkey_path = os.environ.get("SSH_PRIVKEY_PATH")
        self.pubkey_path = "/dev/null"
        if not privkey_path:
            temp_dir = tempfile.mkdtemp(
                prefix="teuthology-ssh-key-",
                dir="/tmp",
            )
            privkey_path = pathlib.Path(temp_dir) / self.__class__.privkey_path
            await self.cmd(
                ["ssh-keygen", "-t", "rsa", "-N", "", "-f", str(privkey_path)],
                check=True,
                force_local=True,
            )
        self.pubkey_path = f"{privkey_path}.pub"
        self.privkey_path = privkey_path


class CephDevStackNetwork(Network):
    _name = "ceph-devstack"


SERVICES: dict[str, Type[StackResource]] = {
    "postgres": Postgres,
    "paddles": Paddles,
    "beanstalk": Beanstalk,
    "pulpito": Pulpito,
    "teuthology": Teuthology,
    "testnode": TestNode,
    "archive": Archive,
    "package_repo": PackageRepo,
    "registry": Registry,
    "ceph_builder": CephBuilder,
    "ceph_node": CephNode,
}

SECRETS = {
    "ssh_keypair": SSHKeyPair,
}


R = TypeVar("R", bound=StackResource)


class CephDevStack:
    def __init__(self, stack_name: str | None = None):
        if stack_name is None:
            stack_name = config.get("stack", "teuthology")
        stacks = config.get("stacks", {})
        if stack_name not in stacks:
            known = ", ".join(sorted(stacks)) or "(none)"
            raise ValueError(f"Unknown stack {stack_name!r}; known stacks: {known}")

        self.stack_name = stack_name
        self.stack_def = stacks[stack_name]
        self.active_services = list(self.stack_def.get("services", []))
        self.local_artifacts = bool(self.stack_def.get("local_artifacts", False))
        self.data_dir = base_data_dir() / stack_name
        # The stack's own view of [containers]; the global config is untouched
        # so that other stacks (e.g. dependencies) cannot leak into this one.
        self.containers_config = deep_merge(
            config.get("containers", {}), self.stack_def.get("containers", {})
        )
        # One ceph_builder view shared by everything that consults its repo,
        # so a distro resolved once is seen everywhere.
        self.builder = CephBuilder(containers_config=self.containers_config)

        self.service_specs = {}
        for name in self.active_services:
            service = SERVICES.get(name)
            if service is None:
                logger.warning(f"Unknown service {name!r} in stack {self.stack_name!r}")
                continue
            count = self.containers_config.get(name, {}).get("count", 1)
            if count == 0:
                continue
            names = [""] if count == 1 else [f"{name}_{i}" for i in range(count)]
            self.service_specs[name] = {
                "objects": [self.make_resource(service, n) for n in names]
            }
        self._wire_services()
        self.secrets = []
        for secret_name in self.stack_def.get("secrets", []):
            if secret_name not in SECRETS:
                logger.warning(
                    f"Unknown secret {secret_name!r} in stack {self.stack_name!r}"
                )
                continue
            self.secrets.append(SECRETS[secret_name])

    def make_resource(self, cls: Type[R], name: str = "") -> R:
        """Instantiate a resource with this stack's data_dir, containers_config
        and active_services."""
        return cls(
            name,
            data_dir=self.data_dir,
            containers_config=self.containers_config,
            active_services=self.active_services,
        )

    def _objects(self) -> Iterator[StackResource]:
        for spec in self.service_specs.values():
            yield from spec["objects"]

    def _first(self, name: str, cls: Type[R]) -> R | None:
        """The (first) resource of service ``name``, if the stack deploys it.

        ``cls`` is the class SERVICES maps ``name`` to; it only types the result.
        """
        spec = self.service_specs.get(name)
        return cast(R, spec["objects"][0]) if spec else None

    def _wire_services(self) -> None:
        postgres_obj = self._first("postgres", Postgres)
        paddles_obj = self._first("paddles", Paddles)
        if postgres_obj and paddles_obj:
            paddles_obj.env_vars["PADDLES_SQLALCHEMY_URL"] = (
                postgres_obj.paddles_sqla_url
            )

        if teuth_obj := self._first("teuthology", Teuthology):
            teuth_obj.use_builder(self.builder, local_artifacts=self.local_artifacts)

    async def check_requirements(self):
        result = True

        result = has_sudo = await HasSudo().evaluate()
        if any(issubclass(SERVICES[n], LoopDeviceMixin) for n in self.service_specs):
            result = result and await LoopControlDeviceExists().evaluate()
            result = result and await LoopControlDeviceWriteable().evaluate()
            result = result and await BlockPoolDiskGroup().evaluate()
            result = result and await BlockPoolParentAccessible().evaluate()

        # Check for SELinux being enabled and Enforcing; then check for the
        # presence of our module. If necessary, inform the user and instruct
        # them how to build and install.
        if has_sudo and await host.selinux_enforcing():
            result = result and await SELinuxModule().evaluate()

        for name in self.service_specs:
            obj = self.containers_config.get(name, {})
            if (repo := obj.get("repo")) and not host.path_exists(
                os.path.expanduser(str(repo))
            ):
                result = False
                logger.error(f"Repo for {name} not found at {repo}")
        return result

    async def apply(self, action: str, **kwargs) -> int | None:
        return await getattr(self, action)(**kwargs)

    async def pull(self):
        logger.info("Pulling images...")
        for spec in self.service_specs.values():
            await spec["objects"][0].pull()

    async def build(self):
        logger.info("Building images...")
        for spec in self.service_specs.values():
            await spec["objects"][0].build()

    async def create(self):
        args = config.get("args", {})
        if args.get("build"):
            await self.build()
        logger.info("Creating containers...")
        await CephDevStackNetwork().create()
        for secret in self.secrets:
            await secret().create()
        # Sequential pre-create steps before the parallel phase:
        # 1. Allocate loop device numbers so concurrent creates don't race.
        # 2. Generate teuthology config (uses podman run to extract the
        #    image's built-in config, which conflicts with concurrent ops).
        for obj in self._objects():
            if isinstance(obj, LoopDeviceMixin):
                obj.allocate_devices()
            if isinstance(obj, Teuthology):
                await obj.generate_config()
        await self._gather_action("create")

    async def _run_depends(self):
        for dep_name in self.stack_def.get("depends", []):
            logger.info(f"Running dependency stack: {dep_name}")
            dep = CephDevStack(stack_name=dep_name)
            await dep.start()

    async def _push_to_registry(self) -> None:
        """Push the locally-built Ceph runtime image to the local registry."""
        registry = self._first("registry", Registry)
        if not registry or not self.builder.repo:
            return
        local_image = self.builder.runtime_image_name
        logger.info(f"Pushing {local_image} to local registry...")
        await registry.push_image(local_image, self.builder)

    async def start(self):
        await self._run_depends()
        await self.create()
        logger.info("Starting containers...")
        for obj in self._objects():
            await obj.start()
        await self._push_to_registry()
        if "teuthology" in self.service_specs:
            logger.info(
                "All containers are running. To monitor teuthology, try running: "
                "podman logs -f teuthology"
            )
        else:
            logger.info("All containers are running.")
        if pulpito := self._first("pulpito", Pulpito):
            logger.info(
                f"View test results at http://{host.hostname()}:{pulpito.port}/"
            )
        if "ceph_node" in self.service_specs:
            logger.info("Monitor the cluster with: podman exec ceph_node ceph -s")

    async def _gather_action(self, action: str):
        await asyncio.gather(*(getattr(obj, action)() for obj in self._objects()))

    async def stop(self):
        logger.info("Stopping containers...")
        await self._gather_action("stop")

    async def remove(self):
        logger.info("Removing containers...")
        await self._gather_action("remove")
        await CephDevStackNetwork().remove()
        for secret in self.secrets:
            await secret().remove()

    async def watch(self):
        logger.info(
            "Entering watch mode: containers will be recreated if removed "
            "and restarted if stopped."
        )
        containers = list(self._objects())
        logger.info(f"Watching {containers}")
        while True:
            for container in containers:
                with contextlib.suppress(CalledProcessError):
                    if not await container.exists():
                        logger.info(
                            f"Container {container.name} was removed; replacing"
                        )
                        await container.create()
                        await container.start()
                    elif not await container.is_running():
                        logger.info(f"Container {container.name} stopped; restarting")
                        await container.start()

    async def wait(self, container_name: str):
        for obj in self._objects():
            if obj.name == container_name:
                return await obj.wait()
        logger.error(f"Could not find container {container_name}")
        return 1

    async def logs(self, run_name: str = "", job_id: str = "", locate: bool = False):
        try:
            log_file = self.get_log_file(run_name, job_id)
        except FileNotFoundError:
            logger.error("No log file found")
        else:
            if locate:
                print(str(log_file.resolve()))
            else:
                buffer_size = 8 * 1024
                with open(log_file) as f:
                    while chunk := f.read(buffer_size):
                        print(chunk, end="")

    def get_log_file(self, run_name: str = "", job_id: str = "") -> pathlib.Path:
        archive_dir = self.data_dir / "archive"

        if not run_name:
            runs = get_runs(archive_dir)
            if not runs:
                raise FileNotFoundError
            run_dir = runs[0]
        else:
            run_dir = archive_dir.joinpath(run_name)

        if not job_id:
            jobs = get_jobs(run_dir)
            if not jobs:
                raise FileNotFoundError
            job_id = jobs[0].name

        log_file = run_dir.joinpath(job_id, "teuthology.log")
        if not log_file.exists():
            raise FileNotFoundError
        return log_file
