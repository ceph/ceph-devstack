import copy
import os
import sys

from pathlib import Path
from typing import List

import yaml

from ceph_devstack import config, deep_merge, DEFAULT_CONFIG_PATH, logger
from ceph_devstack.host import host
from ceph_devstack.resources.ceph.ceph_node import BASE_CAPABILITIES
from ceph_devstack.resources.ceph.host_loops import LoopDeviceMixin
from ceph_devstack.resources.container import Container


ARCHIVE_MOUNT_SUFFIX = "" if sys.platform == "darwin" else ":z"

REGISTRY_CONF_CONTENT = """\
[[registry]]
location = "registry:5000"
insecure = true
"""

LOCAL_ARTIFACT_DEFAULTS = {
    "defaults": {
        "install": {
            "repos": [
                {
                    "name": "local-ceph",
                    "priority": 1,
                    "url": "http://package_repo:8080/rpm/",
                },
            ],
        },
        "cephadm": {
            "containers": {
                "image": "registry:5000/ceph",
            },
        },
    },
}


class Postgres(Container):
    create_cmd = [
        "podman",
        "container",
        "create",
        "-i",
        "--network",
        "ceph-devstack",
        "-p",
        "5432:5432",
        "--health-cmd",
        "CMD pg_isready -q -d paddles -U admin",
        "--health-interval",
        "10s",
        "--health-retries",
        "2",
        "--health-timeout",
        "5s",
        "--name",
        "{name}",
        "{image}",
    ]
    env_vars = {
        "POSTGRES_USER": "root",
        "POSTGRES_PASSWORD": "password",
        "APP_DB_USER": "admin",
        "APP_DB_PASS": "password",
        "APP_DB_NAME": "paddles",
    }

    def __init__(self, name: str = "", **kwargs):
        super().__init__(name, **kwargs)
        username = self.env_vars["APP_DB_USER"]
        password = self.env_vars["APP_DB_PASS"]
        db_name = self.env_vars["APP_DB_NAME"]
        self.paddles_sqla_url = (
            f"postgresql+psycopg2://{username}:{password}@postgres:5432/{db_name}"
        )


class Beanstalk(Container):
    _name = "beanstalk"
    create_cmd = [
        "podman",
        "container",
        "create",
        "-i",
        "--network",
        "ceph-devstack",
        "-p",
        "11300:11300",
        "--name",
        "{name}",
        "{image}",
    ]


class Paddles(Container):
    create_cmd = [
        "podman",
        "container",
        "create",
        "-i",
        "--network",
        "ceph-devstack",
        "-p",
        "8080:8080",
        "--health-cmd",
        "CMD curl -f http://0.0.0.0:8080",
        "--health-interval",
        "10s",
        "--health-retries",
        "30",
        "--health-timeout",
        "5s",
        "--name",
        "{name}",
        "{image}",
    ]
    env_vars = {
        "PADDLES_SERVER_HOST": "0.0.0.0",
        "PADDLES_JOB_LOG_HREF_TEMPL": f"http://{host.hostname()}:8000"
        "/{run_name}/{job_id}/teuthology.log",
    }


class Archive(Container):
    cmd_vars = Container.cmd_vars + ["archive_dir"]
    create_cmd = [
        "podman",
        "container",
        "create",
        "-i",
        "--network",
        "ceph-devstack",
        "-p",
        "8000:8000",
        "-v",
        "{archive_dir}:/archive" + ARCHIVE_MOUNT_SUFFIX,
        "--name",
        "{name}",
        "{image}",
        "python3",
        "-m",
        "http.server",
        "-d",
        "/archive",
    ]

    @property
    def archive_dir(self):
        return self.data_dir / "archive"


class Pulpito(Container):
    create_cmd = [
        "podman",
        "container",
        "create",
        "-i",
        "--network",
        "ceph-devstack",
        "-p",
        "8081:8081",
        "--health-cmd",
        "CMD curl -f http://0.0.0.0:8081",
        "--health-interval",
        "10s",
        "--health-retries",
        "10",
        "--health-timeout",
        "5s",
        "--name",
        "{name}",
        "{image}",
    ]
    env_vars = {
        "PULPITO_PADDLES_ADDRESS": "http://paddles:8080",
        "VITE_MACHINE_TYPE": "testnode",
    }


class TestNode(LoopDeviceMixin, Container):
    _image_name = "teuthology-testnode"
    capabilities = BASE_CAPABILITIES + [
        "SYS_TTY_CONFIG",
        "AUDIT_WRITE",
        "AUDIT_CONTROL",
    ]
    env_vars = {
        "SSH_PUBKEY": "",
        "CEPH_VOLUME_ALLOW_LOOP_DEVICES": "true",
    }

    def __init__(self, name: str = "", **kwargs):
        super().__init__(name=name, **kwargs)
        self.index = 0
        if "_" in self.name:
            self.index = int(self.name.split("_")[-1])
        self._init_loop_devices()

    @property
    def loop_img_dir(self):
        return self.data_dir / "disk_images"

    @property
    def create_cmd(self):
        return [
            "podman",
            "container",
            "create",
            "--rm",
            "-i",
            "--network",
            "ceph-devstack",
            "--systemd=always",
            "--cgroupns=host",
            "--secret",
            "id_rsa.pub",
            "-p",
            "22",
            "--cap-add",
            ",".join(self.capabilities),
            "--security-opt",
            "unmask=/sys/dev/block",
            "-v",
            "/sys/dev/block:/sys/dev/block",
            "-v",
            "/sys/fs/cgroup:/sys/fs/cgroup",
            "-v",
            "/dev/fuse:/dev/fuse",
            "-v",
            "/dev/disk:/dev/disk",
            # cephadm tries to access these DMI-related files, and by default they
            # have 600 permissions on the host. It appears to be ok if they are
            # empty, though.
            # The below was bizarrely causing this error message:
            # No such file or directory: OCI runtime attempted to invoke a command that was
            # not found
            # That was causing the container to fail to start up.
            "-v",
            "/dev/null:/sys/class/dmi/id/board_serial",
            "-v",
            "/dev/null:/sys/class/dmi/id/chassis_serial",
            "-v",
            "/dev/null:/sys/class/dmi/id/product_serial",
            *self.additional_volumes,
            "--device",
            "/dev/net/tun",
            *[f"--device={device}" for device in self.devices],
            "--name",
            "{name}",
            "{image}",
        ]

    @property
    def additional_volumes(self):
        volumes = []
        if (
            sshd_config := DEFAULT_CONFIG_PATH.parent.joinpath("sshd_config")
            .expanduser()
            .absolute()
        ) and sshd_config.exists():
            volumes.extend(
                [
                    "-v",
                    f"{sshd_config}:/etc/ssh/sshd_config.d/teuthology.conf:z",
                ]
            )
        registry_conf = self._registry_conf_path()
        if registry_conf.exists():
            volumes.extend(
                [
                    "-v",
                    f"{registry_conf}:/etc/containers/registries.conf.d/local-registry.conf:z",
                ]
            )
        return volumes

    def _registry_conf_path(self) -> Path:
        return self.data_dir / "local-registry.conf"

    def _write_registry_conf(self):
        """Write insecure registry config so this testnode's podman trusts the local registry."""
        if "registry" not in self.active_services:
            return
        path = self._registry_conf_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(REGISTRY_CONF_CONTENT, encoding="utf-8")

    async def create(self):
        self._write_registry_conf()
        if not await self.exists():
            await self.create_loop_devices()
        await super().create()

    async def remove(self):
        await super().remove()
        await self.remove_loop_devices()


class PackageRepo(Container):
    """HTTP server that serves locally-built packages (RPM now, deb later)."""

    _name = "package_repo"
    cmd_vars = Container.cmd_vars + ["packages_dir"]

    @property
    def config_key(self) -> str:
        return "package_repo"

    @property
    def packages_dir(self) -> Path:
        explicit = self.config.get("packages_dir", "")
        if explicit:
            return Path(explicit).expanduser().absolute()
        builder_cfg = config.get("containers", {}).get("ceph_builder", {})
        repo = builder_cfg.get("repo", "")
        if repo:
            build_subdir = builder_cfg.get("build_dir", "build")
            return (
                Path(repo).expanduser().absolute() / build_subdir / "rpmbuild" / "RPMS"
            )
        return (
            Path(config.get("data_dir", "~/.local/share/ceph-devstack"))
            .expanduser()
            .absolute()
            / "packages"
        )

    @property
    def create_cmd(self):
        packages_dir = self.packages_dir
        return [
            "podman",
            "container",
            "create",
            "-i",
            "--network",
            "ceph-devstack",
            "-v",
            f"{packages_dir}:/packages/rpm" + ARCHIVE_MOUNT_SUFFIX,
            "--name",
            "{name}",
            "{image}",
            "python3",
            "-m",
            "http.server",
            "8080",
            "-d",
            "/packages",
        ]

    async def create(self):
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        await super().create()


class Registry(Container):
    """OCI container registry for serving locally-built images to testnodes."""

    _name = "registry"
    cmd_vars = Container.cmd_vars + ["registry_dir"]

    @property
    def registry_dir(self) -> Path:
        return self.data_dir / "registry"

    @property
    def create_cmd(self):
        registry_dir = self.registry_dir
        return [
            "podman",
            "container",
            "create",
            "-i",
            "--network",
            "ceph-devstack",
            "-v",
            f"{registry_dir}:/var/lib/registry" + ARCHIVE_MOUNT_SUFFIX,
            "--name",
            "{name}",
            "{image}",
        ]

    async def create(self):
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        await super().create()

    async def push_image(self, local_image: str, registry_tag: str = ""):
        """Tag and push a local image to this registry."""
        if not registry_tag:
            tag = local_image.rsplit(":", 1)[-1] if ":" in local_image else "latest"
            registry_tag = f"localhost:5000/ceph:{tag}"
        await self.cmd(
            ["podman", "tag", local_image, registry_tag],
            check=True,
        )
        await self.cmd(
            ["podman", "push", "--tls-verify=false", registry_tag],
            check=True,
            stream_output=True,
        )


class Teuthology(Container):
    cmd_vars: List[str] = ["name", "image", "image_tag", "archive_dir"]

    build_cmd: List[str] = [
        "podman",
        "build",
        "-t",
        "{name}:{image_tag}",
        "-f",
        "./containers/teuthology-dev/Dockerfile",
        ".",
    ]

    @property
    def create_cmd(self):
        cmd = [
            "podman",
            "container",
            "create",
            "-i",
            "--label",
            f"testnode_count={config['containers']['testnode']['count']}",
            "--network",
            "ceph-devstack",
            "--secret",
            "id_rsa",
            "-v",
            "{archive_dir}:/archive_dir" + ARCHIVE_MOUNT_SUFFIX,
        ]
        ansible_inv = os.environ.get("ANSIBLE_INVENTORY_PATH")
        if ansible_inv:
            ansible_inv = Path(ansible_inv).expanduser().absolute()
            cmd += [
                "-v",
                f"{ansible_inv}/inventory:/etc/ansible/hosts",
                "-v",
                f"{ansible_inv}/secrets:/etc/ansible/secrets",
            ]
        ssh_auth_socket = os.environ.get("SSH_AUTH_SOCK")
        if ssh_auth_socket:
            ssh_auth_socket = Path(ssh_auth_socket).expanduser().absolute()
            if ssh_auth_socket.exists():
                cmd += [
                    "-v",
                    f"{ssh_auth_socket}:{ssh_auth_socket}",
                    "-e",
                    f"SSH_AUTH_SOCK={ssh_auth_socket}",
                ]
        custom_conf = os.environ.get("TEUTHOLOGY_CONF")
        if custom_conf:
            custom_conf = Path(custom_conf).expanduser().absolute()
            cmd += [
                "-v",
                f"{custom_conf}:/tmp/conf.yaml",
                "-e",
                "TEUTHOLOGY_CONF=/tmp/conf.yaml",
            ]
        teuthology_yaml = os.environ.get("TEUTHOLOGY_YAML")
        if teuthology_yaml:
            teuthology_yaml = Path(teuthology_yaml).expanduser().absolute()
            cmd += [
                "-v",
                f"{teuthology_yaml}:/root/.teuthology.yaml",
            ]
        elif self._generated_teuthology_yaml.exists():
            cmd += [
                "-v",
                f"{self._generated_teuthology_yaml}:/root/.teuthology.yaml",
            ]
        cmd += [
            "--name",
            "{name}",
            "{image}",
        ]
        return cmd

    env_vars = {
        "SSH_PRIVKEY": "",
        "SSH_PRIVKEY_FILE": "",
        "TEUTHOLOGY_MACHINE_TYPE": "",
        "TEUTHOLOGY_TESTNODES": "",
        "TEUTHOLOGY_BRANCH": "",
        "TEUTHOLOGY_CEPH_BRANCH": "",
        "TEUTHOLOGY_CEPH_REPO": "",
        "TEUTHOLOGY_SUITE": "",
        "TEUTHOLOGY_SUITE_BRANCH": "",
        "TEUTHOLOGY_SUITE_REPO": "",
        "TEUTHOLOGY_SUITE_EXTRA_ARGS": "",
    }

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def _generated_teuthology_yaml(self) -> Path:
        return self.data_dir / "teuthology.yaml"

    async def _extract_builtin_yaml(self) -> dict:
        """Extract the .teuthology.yaml shipped inside the container image."""
        proc = await self.cmd(
            ["podman", "run", "--rm", self.image, "cat", "/root/.teuthology.yaml"],
            check=False,
        )
        stdout = b""
        if proc.stdout:
            stdout = await proc.stdout.read()
        rc = await proc.wait()
        if rc != 0:
            logger.warning(
                f"Could not extract .teuthology.yaml from {self.image} (rc={rc}); "
                "using empty base config"
            )
            return {}
        return yaml.safe_load(stdout.decode()) or {}

    async def _generate_teuthology_yaml(self):
        """Deep-merge local artifact defaults into the image's built-in config."""
        if not (
            "registry" in self.active_services or "package_repo" in self.active_services
        ):
            return
        path = self._generated_teuthology_yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        base_config = await self._extract_builtin_yaml()
        merged = deep_merge(base_config, LOCAL_ARTIFACT_DEFAULTS)
        path.write_text(yaml.dump(merged, default_flow_style=False), encoding="utf-8")
        logger.debug(f"Wrote merged teuthology config to {path}")

    async def create(self):
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        await self._generate_teuthology_yaml()
        await super().create()
