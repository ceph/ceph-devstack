"""Tests for PackageRepo, Registry, and local artifact logic in container classes."""

from unittest.mock import AsyncMock, patch

import pytest
import yaml

from ceph_devstack import config
from ceph_devstack.resources.ceph import CephDevStack
from ceph_devstack.resources.ceph.ceph_builder import CephBuilder
from ceph_devstack.resources.ceph.containers import (
    CONTAINER_CEPH_REPO_PATH,
    CONTAINER_CEPH_REPO_URL,
    PACKAGE_REPO_URL,
    PackageRepo,
    Registry,
    TestNode,
    Teuthology,
)


def make_teuthology_cmd_mock(base_yaml: str, returncode: int = 0):
    """Return a fake ``cmd`` coroutine that simulates ``podman run`` YAML extraction."""

    async def fake_cmd(args, check=False, **kwargs):
        mock_proc = AsyncMock()
        mock_proc.returncode = returncode
        mock_proc.collect_output = AsyncMock(return_value=(base_yaml, ""))
        return mock_proc

    return fake_cmd


def volumes(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]


@pytest.mark.parametrize("cls", [PackageRepo, Registry])
def test_create_cmd_uses_stack_network(cls, tmp_path):
    cmd = cls(data_dir=tmp_path).create_cmd
    assert cmd[cmd.index("--network") + 1] == "ceph-devstack"


class TestPackageRepo:
    def test_create_cmd_includes_volume_mount(self, tmp_path):
        config["containers"]["package_repo"]["packages_dir"] = str(tmp_path)
        cmd = PackageRepo(data_dir=tmp_path).create_cmd
        rpm_mount = [v for v in volumes(cmd) if "/packages/rpm" in v]
        assert len(rpm_mount) == 1
        assert str(tmp_path) in rpm_mount[0]

    def test_create_cmd_runs_http_server(self, tmp_path):
        cmd = PackageRepo(data_dir=tmp_path).create_cmd
        assert "python3" in cmd
        assert "http.server" in cmd
        assert "8080" in cmd

    def test_packages_dir_from_explicit_config(self, tmp_path):
        config["containers"]["package_repo"]["packages_dir"] = str(tmp_path / "pkgs")
        assert PackageRepo(data_dir=tmp_path).packages_dir == tmp_path / "pkgs"

    def test_packages_dir_derived_from_ceph_builder(self, tmp_path, builder_config):
        builder_config(repo=str(tmp_path / "ceph"), build_dir="build")
        config["containers"]["package_repo"].pop("packages_dir", None)
        repo = PackageRepo(data_dir=tmp_path)
        assert repo.packages_dir == tmp_path / "ceph" / "build" / "rpmbuild" / "RPMS"

    def test_packages_dir_matches_builder_for_absolute_build_dir(
        self, tmp_path, builder_config
    ):
        ceph = tmp_path / "ceph"
        builder_config(repo=str(ceph), build_dir=str(ceph / "out"))
        config["containers"]["package_repo"].pop("packages_dir", None)
        repo = PackageRepo(data_dir=tmp_path)
        assert repo.packages_dir == ceph.resolve() / "out" / "rpmbuild" / "RPMS"

    def test_packages_dir_fallback(self, tmp_path):
        config["containers"]["ceph_builder"].pop("repo", None)
        config["containers"]["package_repo"].pop("packages_dir", None)
        repo = PackageRepo(data_dir=tmp_path)
        assert repo.packages_dir == tmp_path / "packages"


class TestRegistry:
    def test_create_cmd_includes_volume_mount(self, tmp_path):
        cmd = Registry(data_dir=tmp_path).create_cmd
        assert len([v for v in volumes(cmd) if "/var/lib/registry" in v]) == 1

    def test_registry_dir_under_data_dir(self, tmp_path):
        assert Registry(data_dir=tmp_path).registry_dir == tmp_path / "registry"


class TestRegistryPushImage:
    @staticmethod
    def podman_calls(reg, verb: str) -> list[list[str]]:
        return [c.args[0] for c in reg.cmd.call_args_list if c.args[0][1] == verb]

    async def test_push_image_tags_and_pushes(self, tmp_path):
        config["containers"]["ceph_builder"].pop("repo", None)
        reg = Registry(data_dir=tmp_path)
        reg.cmd = AsyncMock()
        await reg.push_image("localhost/ceph-devstack:main", CephBuilder())
        assert self.podman_calls(reg, "tag") == [
            [
                "podman",
                "tag",
                "localhost/ceph-devstack:main",
                "localhost:5000/ceph:main",
            ]
        ]
        assert self.podman_calls(reg, "push") == [
            ["podman", "push", "--tls-verify=false", "localhost:5000/ceph:main"]
        ]

    async def test_push_image_adds_ci_tags(self, tmp_path, builder_config, mock_git):
        builder_config(build_distro="centos9")
        reg = Registry(data_dir=tmp_path)
        reg.cmd = AsyncMock()
        await reg.push_image("localhost/ceph-devstack:main", CephBuilder())
        ref = "localhost:5000/ceph:abc123-centos-stream9"
        assert ref in [args[3] for args in self.podman_calls(reg, "tag")]
        assert ref in [args[3] for args in self.podman_calls(reg, "push")]


class TestCephDevStackPushToRegistry:
    async def test_push_to_registry_skipped_without_registry_service(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            """
[stacks.no-reg]
services = ["postgres"]
"""
        )
        config.load(config_file)
        devstack = CephDevStack(stack_name="no-reg")
        await devstack._push_to_registry()

    async def test_push_to_registry_skipped_without_builder_repo(self):
        config["containers"]["ceph_builder"].pop("repo", None)
        devstack = CephDevStack()
        mock_registry = AsyncMock()
        devstack.service_specs["registry"] = {"objects": [mock_registry]}
        await devstack._push_to_registry()
        mock_registry.push_image.assert_not_awaited()

    async def test_push_to_registry_pushes_with_the_stack_builder(self, builder_config):
        builder_config(build_distro="centos9", image_builder="package-build")
        devstack = CephDevStack()
        mock_registry = AsyncMock()
        devstack.service_specs["registry"] = {"objects": [mock_registry]}
        await devstack._push_to_registry()
        mock_registry.push_image.assert_awaited_once_with(
            "localhost/ceph-devstack:main", devstack.builder
        )
        assert devstack.builder.distro == "centos9"


class TestSuiteExtraArgsWiring:
    def test_sets_all_flags_with_local_artifacts(self, builder_config, mock_git):
        builder_config()
        config["stacks"]["teuthology"]["local_artifacts"] = True
        teuth = CephDevStack().service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars.get("TEUTHOLOGY_SUITE_EXTRA_ARGS", "")
        assert "--validate-sha1 false" in extra
        assert "--sha1 abc123" in extra
        assert "--suite-sha1 abc123" in extra
        assert f"--suite-dir {CONTAINER_CEPH_REPO_PATH}" in extra
        assert teuth.env_vars["TEUTHOLOGY_CEPH_REPO"] == CONTAINER_CEPH_REPO_URL
        assert teuth.env_vars["TEUTHOLOGY_SUITE_REPO"] == CONTAINER_CEPH_REPO_URL

    def test_teuthology_shares_the_stack_builder(self, builder_config):
        builder_config()
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        assert teuth.builder is devstack.builder

    def test_uses_stack_level_builder_repo(self, tmp_path):
        config["containers"]["ceph_builder"].pop("repo", None)
        config["stacks"]["teuthology"]["containers"] = {
            "ceph_builder": {"repo": str(tmp_path / "ceph")}
        }
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars["TEUTHOLOGY_SUITE_EXTRA_ARGS"]
        assert f"--suite-dir {CONTAINER_CEPH_REPO_PATH}" in extra
        package_repo = devstack.service_specs["package_repo"]["objects"][0]
        assert package_repo.packages_dir == (tmp_path / "ceph").resolve() / (
            "build/rpmbuild/RPMS"
        )

    def test_does_not_set_flags_without_builder_repo(self):
        config["containers"]["ceph_builder"].pop("repo", None)
        teuth = CephDevStack().service_specs["teuthology"]["objects"][0]
        assert teuth.env_vars.get("TEUTHOLOGY_SUITE_EXTRA_ARGS", "") == ""
        assert teuth.env_vars.get("TEUTHOLOGY_CEPH_REPO", "") == ""
        assert teuth.env_vars.get("TEUTHOLOGY_SUITE_REPO", "") == ""

    def test_preserves_existing_extra_args(self, builder_config, mock_git, monkeypatch):
        builder_config()
        config["stacks"]["teuthology"]["local_artifacts"] = True
        monkeypatch.setenv("TEUTHOLOGY_SUITE_EXTRA_ARGS", "--filter cephadm")
        teuth = CephDevStack().service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars["TEUTHOLOGY_SUITE_EXTRA_ARGS"]
        assert extra.startswith("--filter cephadm ")
        assert "--validate-sha1 false" in extra
        assert "--sha1 abc123" in extra
        assert f"--suite-dir {CONTAINER_CEPH_REPO_PATH}" in extra

    @pytest.mark.parametrize("sep", [" ", "="])
    def test_does_not_duplicate_flags(self, builder_config, mock_git, monkeypatch, sep):
        builder_config()
        config["stacks"]["teuthology"]["local_artifacts"] = True
        user_args = (
            f"--validate-sha1{sep}false --sha1{sep}def456 --suite-sha1{sep}def456"
            f" --suite-dir{sep}/elsewhere"
        )
        monkeypatch.setenv("TEUTHOLOGY_SUITE_EXTRA_ARGS", user_args)
        teuth = CephDevStack().service_specs["teuthology"]["objects"][0]
        assert teuth.env_vars["TEUTHOLOGY_SUITE_EXTRA_ARGS"] == user_args

    def test_user_suite_sha1_does_not_suppress_sha1(
        self, builder_config, mock_git, monkeypatch
    ):
        builder_config()
        config["stacks"]["teuthology"]["local_artifacts"] = True
        monkeypatch.setenv("TEUTHOLOGY_SUITE_EXTRA_ARGS", "--suite-sha1 def456")
        teuth = CephDevStack().service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars["TEUTHOLOGY_SUITE_EXTRA_ARGS"].split()
        assert extra.count("--suite-sha1") == 1
        assert extra[extra.index("--sha1") + 1] == "abc123"


class TestTeuthologyCephRepoMount:
    def test_create_cmd_includes_repo_volume_when_configured(
        self, tmp_path, builder_config, teuthology_services
    ):
        builder_config(repo=str(tmp_path / "ceph"))
        teuth = Teuthology(data_dir=tmp_path, active_services=teuthology_services)
        repo_mount = [
            v for v in volumes(teuth.create_cmd) if CONTAINER_CEPH_REPO_PATH in v
        ]
        assert len(repo_mount) == 1
        assert f":{CONTAINER_CEPH_REPO_PATH}:ro" in repo_mount[0]

    def test_create_cmd_no_repo_volume_without_builder(
        self, tmp_path, teuthology_services
    ):
        config["containers"]["ceph_builder"].pop("repo", None)
        teuth = Teuthology(data_dir=tmp_path, active_services=teuthology_services)
        assert not [
            v for v in volumes(teuth.create_cmd) if CONTAINER_CEPH_REPO_PATH in v
        ]


class TestTestNodeRegistryConf:
    def test_write_registry_conf_creates_file(self, tmp_path, teuthology_services):
        node = TestNode(
            "testnode_0", data_dir=tmp_path, active_services=teuthology_services
        )
        node._write_registry_conf()
        text = node._registry_conf_path().read_text()
        assert "registry:5000" in text
        assert "insecure = true" in text

    def test_write_registry_conf_skipped_without_registry_service(
        self, tmp_path, teuthology_services
    ):
        services = [s for s in teuthology_services if s != "registry"]
        node = TestNode("testnode_0", data_dir=tmp_path, active_services=services)
        node._write_registry_conf()
        assert not node._registry_conf_path().exists()


class TestTeuthologyConfigGeneration:
    @staticmethod
    async def generate(
        tmp_path, services, base: dict, local_artifacts=False, returncode=0
    ) -> Teuthology:
        teuth = Teuthology(data_dir=tmp_path, active_services=services)
        teuth.use_builder(CephBuilder(), local_artifacts=local_artifacts)
        fake_cmd = make_teuthology_cmd_mock(yaml.dump(base), returncode)
        with patch.object(teuth, "cmd", fake_cmd):
            await teuth.generate_config()
        return teuth

    @staticmethod
    def content(teuth: Teuthology) -> dict:
        return yaml.safe_load(teuth._generated_teuthology_yaml.read_text())

    async def test_preserves_base_config(self, tmp_path, teuthology_services):
        base = {
            "queue_host": "beanstalk",
            "queue_port": 11300,
            "lock_server": "http://paddles:8080",
            "results_ui_server": "http://pulpito:8081/",
        }
        teuth = await self.generate(tmp_path, teuthology_services, base)
        assert self.content(teuth) == base

    async def test_includes_local_artifact_defaults(
        self, tmp_path, builder_config, mock_git, teuthology_services
    ):
        builder_config(build_distro="centos9")
        teuth = await self.generate(
            tmp_path, teuthology_services, {"queue_host": "beanstalk"}, True
        )
        content = self.content(teuth)
        defaults = content["defaults"]
        assert defaults["install"]["repos"][0]["name"] == "local-ceph"
        assert defaults["install"]["repos"][0]["url"] == PACKAGE_REPO_URL
        assert (
            defaults["cephadm"]["containers"]["image"]
            == "registry:5000/ceph:abc123-centos-stream9"
        )
        assert content["ceph_git_url"] == CONTAINER_CEPH_REPO_URL
        assert content["ceph_qa_suite_git_url"] == CONTAINER_CEPH_REPO_URL
        assert content["suite_verify_ceph_hash"] is False

    async def test_cephadm_image_uses_the_resolved_distro(
        self, tmp_path, builder_config, mock_git
    ):
        """The registry push and teuthology.yaml must name the same tag."""
        builder_config()  # binary-patch, build_distro unset
        config["stacks"]["teuthology"]["local_artifacts"] = True
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        fake_cmd = make_teuthology_cmd_mock(yaml.dump({"q": 1}))
        with (
            patch.object(CephBuilder, "run_in_image", AsyncMock(return_value="3.12")),
            patch.object(teuth, "cmd", fake_cmd),
        ):
            await teuth.generate_config()
        image = self.content(teuth)["defaults"]["cephadm"]["containers"]["image"]
        assert devstack.builder.distro == "centos10"
        assert image == "registry:5000/ceph:abc123-centos-stream10"

    @pytest.mark.parametrize("service", ["package_repo", "registry"])
    async def test_defaults_only_for_deployed_services(
        self, tmp_path, builder_config, mock_git, teuthology_services, service
    ):
        builder_config()
        other = {"package_repo": "registry", "registry": "package_repo"}[service]
        services = [s for s in teuthology_services if s != other]
        teuth = await self.generate(tmp_path, services, {"q": 1}, True)
        defaults = self.content(teuth)["defaults"]
        assert (
            set(defaults)
            == {"package_repo": {"install"}, "registry": {"cephadm"}}[service]
        )

    async def test_no_artifact_defaults_without_artifact_services(
        self, tmp_path, builder_config, mock_git, teuthology_services
    ):
        builder_config()
        services = [
            s for s in teuthology_services if s not in ("registry", "package_repo")
        ]
        teuth = await self.generate(tmp_path, services, {"queue_host": "x"}, True)
        content = self.content(teuth)
        assert "defaults" not in content
        assert "suite_verify_ceph_hash" not in content

    @pytest.mark.parametrize("stale", [False, True])
    async def test_skips_generation_on_extraction_failure(
        self, tmp_path, teuthology_services, stale
    ):
        path = tmp_path / "teuthology.yaml"
        if stale:
            path.write_text("old config")
        await self.generate(tmp_path, teuthology_services, {}, returncode=1)
        assert not path.exists()


class TestPublishedImagesDefault:
    """With local_artifacts=false (the default) and a ceph_builder repo set,
    the generated .teuthology.yaml gets no artifact overrides, but workers
    still clone ceph from the mounted repo.

    Env-var wiring is covered by TestCrossStackIsolation below.
    """

    async def test_only_git_urls_without_local_artifacts(
        self, tmp_path, builder_config, mock_git, teuthology_services
    ):
        builder_config()
        teuth = Teuthology(data_dir=tmp_path, active_services=teuthology_services)
        teuth.cmd = make_teuthology_cmd_mock(yaml.dump({"queue_host": "beanstalk"}))
        await teuth.generate_config()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert content == {
            "queue_host": "beanstalk",
            "ceph_git_url": CONTAINER_CEPH_REPO_URL,
            "ceph_qa_suite_git_url": CONTAINER_CEPH_REPO_URL,
        }


class TestCrossStackIsolation:
    """ceph_builder.repo configured globally does not inject artifacts
    into the teuthology stack when local_artifacts is false."""

    def test_builder_repo_does_not_contaminate_teuthology_default(
        self, builder_config, mock_git
    ):
        builder_config()
        # local_artifacts defaults to false in config.toml
        config["stacks"]["teuthology"].pop("local_artifacts", None)
        teuth = CephDevStack().service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars.get("TEUTHOLOGY_SUITE_EXTRA_ARGS", "")
        assert "--sha1" not in extra
        assert "--validate-sha1" not in extra
        assert teuth.env_vars.get("TEUTHOLOGY_CEPH_REPO", "") == ""
        assert teuth.env_vars.get("TEUTHOLOGY_SUITE_REPO", "") == ""
        # But suite-dir IS set (repo mount for discovery)
        assert f"--suite-dir {CONTAINER_CEPH_REPO_PATH}" in extra
