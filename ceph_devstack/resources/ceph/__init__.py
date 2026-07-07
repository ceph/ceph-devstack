import asyncio
import contextlib
import os
import pathlib
import tempfile

from subprocess import CalledProcessError

from ceph_devstack import config, logger
from ceph_devstack.host import host
from ceph_devstack.resources.misc import Secret, Network
from ceph_devstack.resources.ceph.containers import (
    Postgres,
    Beanstalk,
    Paddles,
    Pulpito,
    TestNode,
    Teuthology,
    Archive,
)
from ceph_devstack.resources.ceph.ceph_node import CephNode, CONTAINER_CLUSTER_DIR
from ceph_devstack.resources.ceph.requirements import (
    HasSudo,
    LoopControlDeviceExists,
    LoopControlDeviceWriteable,
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


SERVICES = {
    "postgres": Postgres,
    "paddles": Paddles,
    "beanstalk": Beanstalk,
    "pulpito": Pulpito,
    "teuthology": Teuthology,
    "testnode": TestNode,
    "archive": Archive,
    "ceph_node": CephNode,
}

SECRETS = {
    "ssh_keypair": SSHKeyPair,
}


class CephDevStack:
    networks = [CephDevStackNetwork]
    secrets = [SSHKeyPair]

    def __init__(self, stack_name: str | None = None):
        if (
            stack_name and stack_name != config.active_stack
        ) or config.active_stack is None:
            config.apply_stack(stack_name)

        self.stack_name = config.active_stack
        self.service_specs = {}
        for name in config.active_services:
            service = SERVICES.get(name)
            if service is None:
                logger.warning(f"Unknown service {name!r} in stack {self.stack_name!r}")
                continue
            count = config["containers"][name].get("count", 1)
            if count == 0:
                continue
            self.service_specs[name] = {
                "obj": service,
                "count": count,
            }
            if count == 1:
                self.service_specs[name]["objects"] = [service()]
            elif count > 1:
                self.service_specs[name]["objects"] = [
                    service(name=f"{name}_{i}") for i in range(count)
                ]
        self._wire_services()
        stack = config.get("stacks", {}).get(self.stack_name, {})
        self.secrets = [
            SECRETS[secret_name]
            for secret_name in stack.get("secrets", [])
            if secret_name in SECRETS
        ]

    def _wire_services(self):
        if (postgres_spec := self.service_specs.get("postgres")) and (
            paddles_spec := self.service_specs.get("paddles")
        ):
            postgres_obj = postgres_spec["objects"][0]
            paddles_obj = paddles_spec["objects"][0]
            paddles_obj.env_vars["PADDLES_SQLALCHEMY_URL"] = (
                postgres_obj.paddles_sqla_url
            )

    async def check_requirements(self):
        result = True

        result = has_sudo = await HasSudo().evaluate()
        if "testnode" in self.service_specs or "ceph_node" in self.service_specs:
            result = result and await LoopControlDeviceExists().evaluate()
            result = result and await LoopControlDeviceWriteable().evaluate()

        # Check for SELinux being enabled and Enforcing; then check for the
        # presence of our module. If necessary, inform the user and instruct
        # them how to build and install.
        if has_sudo and await host.selinux_enforcing():
            result = result and await SELinuxModule().evaluate()

        for name in self.service_specs:
            obj = config["containers"][name]
            if (repo := obj.get("repo")) and not host.path_exists(
                os.path.expanduser(str(repo))
            ):
                result = False
                logger.error(f"Repo for {name} not found at {repo}")
        return result

    async def apply(self, action: str, **kwargs) -> int | None:
        if action == "wait":
            return await self.wait(**kwargs)
        if action == "logs":
            return await self.logs(**kwargs)
        method = getattr(self, action, None)
        if method is None:
            raise AttributeError(f"Unknown action {action!r}")
        return await method()

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
        tasks = []
        for spec in self.service_specs.values():
            for object in spec["objects"]:
                tasks.append(object.create())
        await asyncio.gather(*tasks)

    async def start(self):
        await self.create()
        logger.info("Starting containers...")
        for spec in self.service_specs.values():
            for object in spec["objects"]:
                await object.start()
        if "teuthology" in self.service_specs:
            logger.info(
                "All containers are running. To monitor teuthology, try running: "
                "podman logs -f teuthology"
            )
        else:
            logger.info("All containers are running.")
        if "pulpito" in self.service_specs:
            hostname = host.hostname()
            logger.info(f"View test results at http://{hostname}:8081/")
        if "ceph_node" in self.service_specs:
            logger.info(
                "Monitor the cluster with: podman exec ceph_node ceph "
                f"-c {CONTAINER_CLUSTER_DIR}/ceph.conf -s"
            )

    async def stop(self):
        logger.info("Stopping containers...")
        containers = []
        for spec in self.service_specs.values():
            for object in spec["objects"]:
                containers.append(object.stop())
        await asyncio.gather(*containers)

    async def remove(self):
        logger.info("Removing containers...")
        containers = []
        for spec in self.service_specs.values():
            for object in spec["objects"]:
                containers.append(object.remove())
        await asyncio.gather(*containers)
        await CephDevStackNetwork().remove()
        for secret in self.secrets:
            await secret().remove()

    async def watch(self):
        logger.info(
            "Entering watch mode: while waiting for teuthology to "
            "exit, other containers will be replaced as they are stopped."
        )
        containers = []
        for spec in self.service_specs.values():
            if not spec["count"] > 0:
                continue
            for object in spec["objects"]:
                containers.append(object)
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
        for spec in self.service_specs.values():
            for object in spec["objects"]:
                if object.name == container_name:
                    return await object.wait()
        logger.error(f"Could not find container {container_name}")
        return 1

    async def logs(self, run_name: str = "", job_id: str = "", locate: bool = False):
        try:
            log_file = self.get_log_file(run_name, job_id)
        except FileNotFoundError:
            logger.error("No log file found")
        else:
            if locate:
                print(str(log_file).replace(str(pathlib.Path.home()), "~"))
            else:
                buffer_size = 8 * 1024
                with open(log_file) as f:
                    while chunk := f.read(buffer_size):
                        print(chunk, end="")

    def get_log_file(self, run_name: str = "", job_id: str = "") -> pathlib.Path:
        archive_dir = Teuthology().archive_dir

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
