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
from ceph_devstack.resources.ceph.requirements import (
    HasSudo,
    LoopControlDeviceExists,
    LoopControlDeviceWriteable,
    SELinuxModule,
)
from ceph_devstack.resources.ceph.utils import get_runs
from ceph_devstack.resources.ceph.exceptions import TooManyJobsFound


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


class CephDevStack:
    networks = [CephDevStackNetwork]
    secrets = [SSHKeyPair]

    def __init__(self):
        services = [
            Postgres,
            Paddles,
            Beanstalk,
            Pulpito,
            Teuthology,
            TestNode,
            Archive,
        ]
        self.service_specs = {}
        for service in services:
            name = service.__name__.lower()
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
        if postgres_spec := self.service_specs.get("postgres"):
            postgres_obj = postgres_spec["objects"][0]
            paddles_obj = self.service_specs["paddles"]["objects"][0]
            paddles_obj.env_vars["PADDLES_SQLALCHEMY_URL"] = (
                postgres_obj.paddles_sqla_url
            )

    async def check_requirements(self):
        result = True

        result = has_sudo = await HasSudo().evaluate()
        result = result and await LoopControlDeviceExists().evaluate()
        result = result and await LoopControlDeviceWriteable().evaluate()

        # Check for SELinux being enabled and Enforcing; then check for the
        # presence of our module. If necessary, inform the user and instruct
        # them how to build and install.
        if has_sudo and await host.selinux_enforcing():
            result = result and await SELinuxModule().evaluate()

        for name, obj in config["containers"].items():
            if (repo := obj.get("repo")) and not host.path_exists(repo):
                result = False
                logger.error(f"Repo for {name} not found at {repo}")
        return result

    async def apply(self, action):
        return await getattr(self, action)()

    async def pull(self):
        logger.info("Pulling images...")
        for spec in self.service_specs.values():
            await spec["objects"][0].pull()

    async def build(self):
        logger.info("Building images...")
        for spec in self.service_specs.values():
            await spec["objects"][0].build()

    async def create(self):
        logger.info("Creating containers...")
        await CephDevStackNetwork().create()
        await SSHKeyPair().create()
        containers = []
        for spec in self.service_specs.values():
            for object in spec["objects"]:
                containers.append(object.create())
        await asyncio.gather(*containers)

    async def start(self):
        await self.create()
        logger.info("Starting containers...")
        for spec in self.service_specs.values():
            for object in spec["objects"]:
                await object.start()
        logger.info(
            "All containers are running. To monitor teuthology, try running: podman "
            "logs -f teuthology"
        )
        hostname = host.hostname()
        logger.info(f"View test results at http://{hostname}:8081/")

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
        await SSHKeyPair().remove()

    async def watch(self):
        logger.info("Watching containers; will replace any that are stopped")
        containers = []
        for spec in self.service_specs.values():
            if not spec["count"] > 0:
                continue
            for object in spec["objects"]:
                containers.append(object)
        logger.info(f"Watching {containers}")
        while True:
            try:
                for container in containers:
                    with contextlib.suppress(CalledProcessError):
                        if not await container.exists():
                            logger.info(
                                f"Container {container.name} was removed; replacing"
                            )
                            await container.create()
                            await container.start()
                        elif not await container.is_running():
                            logger.info(
                                f"Container {container.name} stopped; restarting"
                            )
                            await container.start()
            except KeyboardInterrupt:
                break

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
        except TooManyJobsFound as e:
            msg = "Found too many jobs ({jobs}) for target run. Please pick a job id with -j option.".format(
                jobs=", ".join(e.jobs)
            )
            logger.error(msg)
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
            jobs = sorted(
                [dir_.name for dir_ in run_dir.iterdir() if str(dir_.name).isdigit()]
            )
            if not jobs:
                raise FileNotFoundError
            job_id = jobs[0]

        log_file = run_dir.joinpath(job_id, "teuthology.log")
        if not log_file.exists():
            raise FileNotFoundError
        return log_file
