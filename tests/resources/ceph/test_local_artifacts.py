"""Tests for PackageRepo, Registry, and local artifact logic in container classes."""

from unittest.mock import AsyncMock

import pytest
import yaml

from ceph_devstack import config
from ceph_devstack.resources.ceph.containers import (
    PackageRepo,
    Registry,
    TestNode,
    Teuthology,
)
from tests.resources.ceph.conftest import TEUTHOLOGY_SERVICES


def make_teuthology_cmd_mock(base_yaml: str):
    """Return a fake ``cmd`` coroutine that simulates ``podman run`` YAML extraction."""

    async def fake_cmd(args, check=False, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.collect_output = AsyncMock(return_value=(base_yaml, ""))
        return mock_proc

    return fake_cmd


class TestPackageRepo:
    def test_create_cmd_includes_network(self, tmp_path):
        repo = PackageRepo(data_dir=tmp_path)
        cmd = repo.create_cmd
        assert "--network" in cmd
        idx = cmd.index("--network")
        assert cmd[idx + 1] == "ceph-devstack"

    def test_create_cmd_includes_volume_mount(self, tmp_path):
        config["containers"]["package_repo"]["packages_dir"] = str(tmp_path)
        repo = PackageRepo(data_dir=tmp_path)
        cmd = repo.create_cmd
        volume_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        rpm_mount = [v for v in volume_args if "/packages/rpm" in v]
        assert len(rpm_mount) == 1
        assert str(tmp_path) in rpm_mount[0]

    def test_create_cmd_runs_http_server(self, tmp_path):
        repo = PackageRepo(data_dir=tmp_path)
        cmd = repo.create_cmd
        assert "python3" in cmd
        assert "http.server" in cmd
        assert "8080" in cmd

    def test_packages_dir_from_explicit_config(self, tmp_path):
        config["containers"]["package_repo"]["packages_dir"] = str(tmp_path / "pkgs")
        repo = PackageRepo(data_dir=tmp_path)
        assert repo.packages_dir == tmp_path / "pkgs"

    def test_packages_dir_derived_from_ceph_builder(self, tmp_path):
        config["containers"]["ceph_builder"]["repo"] = str(tmp_path / "ceph")
        config["containers"]["ceph_builder"]["build_dir"] = "build"
        config["containers"]["package_repo"].pop("packages_dir", None)
        repo = PackageRepo(data_dir=tmp_path)
        assert repo.packages_dir == tmp_path / "ceph" / "build" / "rpmbuild" / "RPMS"

    def test_packages_dir_fallback(self, tmp_path):
        config["containers"]["ceph_builder"].pop("repo", None)
        config["containers"]["package_repo"].pop("packages_dir", None)
        config["data_dir"] = str(tmp_path)
        repo = PackageRepo(data_dir=tmp_path)
        assert repo.packages_dir == tmp_path / "packages"


class TestRegistry:
    def test_create_cmd_includes_network(self, tmp_path):
        reg = Registry(data_dir=tmp_path)
        cmd = reg.create_cmd
        assert "--network" in cmd
        idx = cmd.index("--network")
        assert cmd[idx + 1] == "ceph-devstack"

    def test_create_cmd_includes_volume_mount(self, tmp_path):
        reg = Registry(data_dir=tmp_path)
        cmd = reg.create_cmd
        volume_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        registry_mount = [v for v in volume_args if "/var/lib/registry" in v]
        assert len(registry_mount) == 1

    def test_registry_dir_under_data_dir(self, tmp_path):
        reg = Registry(data_dir=tmp_path)
        assert reg.registry_dir == tmp_path / "registry"


class TestTestNodeRegistryConf:
    def test_write_registry_conf_creates_file(self, tmp_path):
        node = TestNode(
            "testnode_0",
            data_dir=tmp_path,
            active_services=TEUTHOLOGY_SERVICES,
        )
        node._write_registry_conf()
        path = node._registry_conf_path()
        assert path.exists()
        assert "registry:5000" in path.read_text()
        assert "insecure = true" in path.read_text()

    def test_write_registry_conf_skipped_without_registry_service(self, tmp_path):
        services_without_registry = [s for s in TEUTHOLOGY_SERVICES if s != "registry"]
        node = TestNode(
            "testnode_0",
            data_dir=tmp_path,
            active_services=services_without_registry,
        )
        node._write_registry_conf()
        path = node._registry_conf_path()
        assert not path.exists()


class TestTeuthologyYamlGeneration:
    @pytest.fixture
    def mock_cmd(self):
        base_config = yaml.dump(
            {
                "queue_host": "beanstalk",
                "queue_port": 11300,
                "lock_server": "http://paddles:8080",
                "results_server": "http://paddles:8080",
            }
        ).encode()

        async def fake_cmd(args, check=False, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.stdout = AsyncMock()
            mock_proc.stdout.read = AsyncMock(return_value=base_config)
            mock_proc.wait = AsyncMock(return_value=0)
            return mock_proc

        return fake_cmd

    async def test_generates_merged_file(self, tmp_path, mock_cmd):
        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = mock_cmd
        await teuth._generate_teuthology_yaml()
        assert teuth._generated_teuthology_yaml.exists()

    async def test_preserves_base_config(self, tmp_path, mock_cmd):
        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = mock_cmd
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert content["queue_host"] == "beanstalk"
        assert content["lock_server"] == "http://paddles:8080"

    async def test_includes_local_artifact_defaults(self, tmp_path, mock_cmd):
        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = mock_cmd
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        defaults = content["defaults"]
        assert defaults["install"]["repos"][0]["name"] == "local-ceph"
        assert defaults["install"]["repos"][0]["url"] == "http://package_repo:8080/rpm/"
        assert defaults["cephadm"]["containers"]["image"] == "registry:5000/ceph"

    async def test_skipped_without_artifact_services(self, tmp_path):
        services_no_artifacts = [
            s for s in TEUTHOLOGY_SERVICES if s not in ("registry", "package_repo")
        ]
        teuth = Teuthology(data_dir=tmp_path, active_services=services_no_artifacts)
        await teuth._generate_teuthology_yaml()
        assert not teuth._generated_teuthology_yaml.exists()

    async def test_merges_existing_defaults(self, tmp_path):
        base_config = yaml.dump(
            {
                "queue_host": "beanstalk",
                "defaults": {"existing_key": "existing_value"},
            }
        ).encode()

        async def fake_cmd(args, check=False, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.stdout = AsyncMock()
            mock_proc.stdout.read = AsyncMock(return_value=base_config)
            mock_proc.wait = AsyncMock(return_value=0)
            return mock_proc

        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = fake_cmd
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert content["defaults"]["existing_key"] == "existing_value"
        assert "install" in content["defaults"]
        assert "cephadm" in content["defaults"]
