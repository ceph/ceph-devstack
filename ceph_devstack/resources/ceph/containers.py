import os
import sys

from pathlib import Path
from typing import List

import yaml

from ceph_devstack import DEFAULT_CONFIG_PATH, deep_merge, logger
from ceph_devstack.host import host
from ceph_devstack.resources.ceph.ceph_builder import CephBuilder
from ceph_devstack.resources.ceph.host_loops import LoopDeviceMixin
from ceph_devstack.resources.container import Container
from ceph_devstack.resources.utils import git_sha1


# Bind-mount option that has podman relabel the source for SELinux (shared).
# Chosen from the local platform, so with a RemoteHost it describes this
# machine rather than the podman host.
SELINUX_RELABEL_SUFFIX = "" if sys.platform == "darwin" else ":z"
# The same, for a read-only bind mount.
SELINUX_RELABEL_RO_SUFFIX = ":ro" + SELINUX_RELABEL_SUFFIX.replace(":", ",")

CONTAINER_CEPH_REPO_PATH = "/ceph_repo"
CONTAINER_CEPH_REPO_URL = f"file://{CONTAINER_CEPH_REPO_PATH}"

REGISTRY_PORT = 5000
# The registry as testnodes and teuthology see it on the ceph-devstack network.
REGISTRY_ADDRESS = f"registry:{REGISTRY_PORT}"
# Where the host pushes locally-built Ceph images.
LOCAL_REGISTRY_REPO = f"localhost:{REGISTRY_PORT}/ceph"

PACKAGE_REPO_PORT = 8080
PACKAGE_REPO_URL = f"http://package_repo:{PACKAGE_REPO_PORT}/rpm/"

REGISTRY_CONF_CONTENT = f"""\
[[registry]]
location = "{REGISTRY_ADDRESS}"
insecure = true
"""


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
        "{archive_dir}:/archive" + SELINUX_RELABEL_SUFFIX,
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
    port = 8081
    cmd_vars = Container.cmd_vars + ["port"]
    create_cmd = [
        "podman",
        "container",
        "create",
        "-i",
        "--network",
        "ceph-devstack",
        "-p",
        "{port}:{port}",
        "--health-cmd",
        "CMD curl -f http://0.0.0.0:{port}",
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
    capabilities = [
        "SYS_ADMIN",
        "NET_ADMIN",
        "SYS_TIME",
        "SYS_RAWIO",
        "MKNOD",
        "NET_RAW",
        "SETUID",
        "SETGID",
        "CHOWN",
        "SYS_PTRACE",
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
            "-v",
            "/run/udev:/run/udev:ro",
            *self._get_available_dmi_mounts(),
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
                    f"{registry_conf}:/etc/containers/registries.conf.d/local-registry.conf"
                    + SELINUX_RELABEL_SUFFIX,
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

    def _get_available_dmi_mounts(self) -> List[str]:
        """Return volume mount args for DMI files that exist on the host.

        cephadm tries to access DMI-related files, and by default they have 600
        permissions on the host. We mount /dev/null to make them accessible but empty.
        However, in nested virtualization these files may not exist at all, so we
        only mount them if they're present on the host.
        """
        dmi_files = [
            "board_serial",
            "chassis_serial",
            "product_serial",
        ]
        mounts = []
        for dmi_file in dmi_files:
            dmi_path = f"/sys/class/dmi/id/{dmi_file}"
            if host.path_exists(dmi_path):
                mounts.extend(["-v", f"/dev/null:{dmi_path}"])
        return mounts


class PackageRepo(Container):
    """HTTP server that serves locally-built packages (RPM now, deb later)."""

    _name = "package_repo"
    cmd_vars = Container.cmd_vars + ["packages_dir"]

    @property
    def packages_dir(self) -> Path:
        explicit = self.config.get("packages_dir", "")
        if explicit:
            return Path(explicit).expanduser().absolute()
        builder = CephBuilder(containers_config=self.containers_config)
        if builder.repo:
            return builder.build_path / "rpmbuild" / "RPMS"
        return self.data_dir / "packages"

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
            f"{packages_dir}:/packages/rpm" + SELINUX_RELABEL_SUFFIX,
            "--name",
            "{name}",
            "{image}",
            "python3",
            "-m",
            "http.server",
            str(PACKAGE_REPO_PORT),
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
            "-p",
            f"{REGISTRY_PORT}:{REGISTRY_PORT}",
            "--health-cmd",
            f"CMD wget -q --spider http://localhost:{REGISTRY_PORT}/v2/",
            "--health-interval",
            "5s",
            "--health-retries",
            "3",
            "--health-timeout",
            "3s",
            "-v",
            f"{registry_dir}:/var/lib/registry" + SELINUX_RELABEL_SUFFIX,
            "--name",
            "{name}",
            "{image}",
        ]

    async def create(self):
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        await super().create()

    async def push_image(self, local_image: str, builder: CephBuilder):
        """Push a local image to this registry under its own tag.

        Also pushes the CI-pattern tags (full, branch, sha1) for the
        builder's repo, so cephadm can locate images by commit hash.
        """
        tag = local_image.rsplit(":", 1)[-1] if ":" in local_image else "latest"
        await self._tag_and_push(local_image, f"{LOCAL_REGISTRY_REPO}:{tag}")
        for tag_name, tag_value in builder.ci_image_tags().items():
            logger.info(f"Pushing {tag_name} tag: {LOCAL_REGISTRY_REPO}:{tag_value}")
            await self._tag_and_push(local_image, f"{LOCAL_REGISTRY_REPO}:{tag_value}")

    async def _tag_and_push(self, local_image: str, ref: str):
        await self.cmd(["podman", "tag", local_image, ref], check=True)
        await self.cmd(
            ["podman", "push", "--tls-verify=false", ref],
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
            f"testnode_count={self.containers_config['testnode']['count']}",
            "--network",
            "ceph-devstack",
            "--secret",
            "id_rsa",
            "-v",
            "{archive_dir}:/archive_dir" + SELINUX_RELABEL_SUFFIX,
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
                f"{teuthology_yaml}:/root/.teuthology.yaml" + SELINUX_RELABEL_SUFFIX,
            ]
        elif self._generated_teuthology_yaml.exists():
            cmd += [
                "-v",
                f"{self._generated_teuthology_yaml}:/root/.teuthology.yaml"
                + SELINUX_RELABEL_SUFFIX,
            ]
        if repo_path := self.builder.repo:
            cmd += [
                "-v",
                f"{repo_path}:{CONTAINER_CEPH_REPO_PATH}{SELINUX_RELABEL_RO_SUFFIX}",
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

    def __init__(self, name: str = "", **kwargs):
        super().__init__(name, **kwargs)
        self.builder = CephBuilder(containers_config=self.containers_config)
        self.local_artifacts = False

    def use_builder(self, builder: CephBuilder, local_artifacts: bool) -> None:
        """Adopt the stack's ceph_builder and local_artifacts setting.

        With a builder repo, it is mounted at CONTAINER_CEPH_REPO_PATH and
        suites are read from it (--suite-dir); generate_config() also lets
        workers clone ceph from it. Scheduling still resolves the sha1
        upstream unless local_artifacts is set, in which case teuthology
        tests the repo's own commit (--sha1, --suite-sha1, no shaman
        validation) and uses it as TEUTHOLOGY_CEPH_REPO/SUITE_REPO.
        """
        self.builder = builder
        self.local_artifacts = local_artifacts
        if not builder.repo:
            return
        suite_args = {"--suite-dir": CONTAINER_CEPH_REPO_PATH}
        if local_artifacts:
            suite_args["--validate-sha1"] = "false"
            if sha1 := git_sha1(builder.repo):
                suite_args["--sha1"] = sha1
                suite_args["--suite-sha1"] = sha1
            self._default_env("TEUTHOLOGY_CEPH_REPO", CONTAINER_CEPH_REPO_URL)
            self._default_env("TEUTHOLOGY_SUITE_REPO", CONTAINER_CEPH_REPO_URL)
        self._add_suite_args(suite_args)

    def _default_env(self, key: str, value: str) -> None:
        """Set an env var unless the user's environment already did."""
        if not self.env_vars.get(key):
            self.env_vars[key] = value

    def _add_suite_args(self, args: dict[str, str]) -> None:
        """Append to TEUTHOLOGY_SUITE_EXTRA_ARGS the flags not already in it."""
        extra = self.env_vars.get("TEUTHOLOGY_SUITE_EXTRA_ARGS") or ""
        # A user may have given a flag as "--flag value" or "--flag=value".
        present = {token.split("=", 1)[0] for token in extra.split()}
        flags = [
            f"{flag} {value}" for flag, value in args.items() if flag not in present
        ]
        if flags:
            self.env_vars["TEUTHOLOGY_SUITE_EXTRA_ARGS"] = " ".join(
                [extra, *flags]
            ).strip()

    @property
    def archive_dir(self) -> Path:
        return self.data_dir / "archive"

    @property
    def _generated_teuthology_yaml(self) -> Path:
        return self.data_dir / "teuthology.yaml"

    async def _extract_builtin_yaml(self) -> dict:
        """Extract the .teuthology.yaml shipped inside the container image."""
        out = await self.run_in_image(
            self.image, "/bin/bash", "-c", "cat /root/.teuthology.yaml"
        )
        if out is None:
            logger.warning(f"Could not extract .teuthology.yaml from {self.image}")
            return {}
        return yaml.safe_load(out) or {}

    async def _local_artifact_defaults(self) -> dict:
        """teuthology.yaml defaults that point jobs at this stack's artifacts."""
        defaults: dict = {}
        if "package_repo" in self.active_services:
            defaults["install"] = {
                "repos": [
                    {"name": "local-ceph", "priority": 1, "url": PACKAGE_REPO_URL}
                ]
            }
        if "registry" in self.active_services:
            # The tag must name the same distro as the pushed image's tags.
            await self.builder.resolve_distro()
            sha1_tag = self.builder.ci_image_tags().get("sha1")
            if sha1_tag:
                defaults["cephadm"] = {
                    "cephadm_from_container": True,
                    "containers": {"image": f"{REGISTRY_ADDRESS}/ceph:{sha1_tag}"},
                }
            else:
                logger.warning(
                    "No ceph_builder repo commit; cephadm will not use an image "
                    "from the local registry"
                )
        return defaults

    async def generate_config(self) -> None:
        """Write the teuthology.yaml that create_cmd mounts.

        It is the config shipped in the image, plus (see use_builder())
        ceph_git_url/ceph_qa_suite_git_url pointing at a mounted builder
        repo and, with local_artifacts and package_repo or registry
        deployed, defaults that use them and no suite_verify_ceph_hash.
        """
        path = self._generated_teuthology_yaml
        conf = await self._extract_builtin_yaml()
        if not conf:
            logger.warning(
                "Skipping teuthology config generation; the container's "
                "built-in config will be used without local artifact settings"
            )
            path.unlink(missing_ok=True)
            return
        if self.builder.repo:
            conf["ceph_git_url"] = CONTAINER_CEPH_REPO_URL
            conf["ceph_qa_suite_git_url"] = CONTAINER_CEPH_REPO_URL
        if self.local_artifacts and (
            {"package_repo", "registry"} & set(self.active_services)
        ):
            if defaults := await self._local_artifact_defaults():
                conf = deep_merge(conf, {"defaults": defaults})
            conf["suite_verify_ceph_hash"] = False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(conf, default_flow_style=False), encoding="utf-8")

    async def create(self):
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        await super().create()
