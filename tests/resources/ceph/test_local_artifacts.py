"""Tests for PackageRepo, Registry, and local artifact logic in container classes."""

from unittest.mock import AsyncMock, patch
import yaml

from ceph_devstack import DEFAULT_CEPH_IMAGE, config
from ceph_devstack.resources.ceph import CephDevStack
from ceph_devstack.resources.ceph.containers import (
    CONTAINER_CEPH_REPO_PATH,
    CONTAINER_CEPH_REPO_URL,
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


class TestRegistryPushImage:
    async def test_push_image_tags_and_pushes(self, tmp_path):
        reg = Registry(data_dir=tmp_path)
        reg.cmd = AsyncMock()
        await reg.push_image("localhost/ceph-devstack:main")
        calls = reg.cmd.call_args_list
        tag_call = calls[0]
        assert tag_call[0][0] == [
            "podman",
            "tag",
            "localhost/ceph-devstack:main",
            "localhost:5000/ceph:main",
        ]
        push_call = calls[1]
        assert push_call[0][0] == [
            "podman",
            "push",
            "--tls-verify=false",
            "localhost:5000/ceph:main",
        ]

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    @patch("ceph_devstack.resources.utils.git_branch", return_value="main")
    async def test_push_image_adds_ci_tags_when_repo_configured(
        self, _mock_branch, _mock_sha1, tmp_path
    ):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "build_distro": "centos9",
        }
        reg = Registry(data_dir=tmp_path)
        reg.cmd = AsyncMock()
        await reg.push_image("localhost/ceph-devstack:main")
        tag_args = [
            c[0][0]
            for c in reg.cmd.call_args_list
            if c[0][0][0] == "podman" and c[0][0][1] == "tag"
        ]
        pushed_refs = [
            c[0][0][3]
            for c in reg.cmd.call_args_list
            if c[0][0][0] == "podman" and c[0][0][1] == "push"
        ]
        assert "localhost:5000/ceph:abc123-centos-stream9" in [
            args[3] for args in tag_args
        ]
        assert "localhost:5000/ceph:abc123-centos-stream9" in pushed_refs

    async def test_push_image_uses_explicit_registry_tag(self, tmp_path):
        reg = Registry(data_dir=tmp_path)
        reg.cmd = AsyncMock()
        await reg.push_image(
            "localhost/ceph-devstack:main",
            registry_tag="localhost:5000/ceph:custom",
        )
        tag_call = reg.cmd.call_args_list[0]
        assert tag_call[0][0][3] == "localhost:5000/ceph:custom"


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
        await devstack._push_to_registry()

    async def test_push_to_registry_pushes_when_repo_configured(self, tmp_path):
        config["containers"]["ceph_builder"] = {
            "repo": str(tmp_path / "fake-ceph"),
            "build_distro": "centos9",
            "target_image": DEFAULT_CEPH_IMAGE,
            "image_builder": "package-build",
        }
        devstack = CephDevStack()
        mock_registry = AsyncMock()
        devstack.service_specs["registry"] = {
            "count": 1,
            "objects": [mock_registry],
        }
        await devstack._push_to_registry()
        mock_registry.push_image.assert_awaited_once_with(
            "localhost/ceph-devstack:main", distro="centos9"
        )


class TestSuiteExtraArgsWiring:
    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    def test_sets_all_flags_when_builder_repo_configured(self, _mock_sha1):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "build_distro": "centos9",
            "image_builder": "package-build",
        }
        config["stacks"]["teuthology"]["local_artifacts"] = True
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars.get("TEUTHOLOGY_SUITE_EXTRA_ARGS", "")
        assert "--validate-sha1 false" in extra
        assert "--sha1 abc123" in extra
        assert "--suite-sha1 abc123" in extra
        assert f"--suite-dir {CONTAINER_CEPH_REPO_PATH}" in extra
        assert teuth.env_vars["TEUTHOLOGY_CEPH_REPO"] == CONTAINER_CEPH_REPO_URL
        assert teuth.env_vars["TEUTHOLOGY_SUITE_REPO"] == CONTAINER_CEPH_REPO_URL

    def test_does_not_set_flags_without_builder_repo(self):
        config["containers"]["ceph_builder"].pop("repo", None)
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        assert teuth.env_vars.get("TEUTHOLOGY_SUITE_EXTRA_ARGS", "") == ""
        assert teuth.env_vars.get("TEUTHOLOGY_CEPH_REPO", "") == ""
        assert teuth.env_vars.get("TEUTHOLOGY_SUITE_REPO", "") == ""

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    def test_preserves_existing_extra_args(self, _mock_sha1, monkeypatch):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "image_builder": "package-build",
        }
        config["stacks"]["teuthology"]["local_artifacts"] = True
        monkeypatch.setenv("TEUTHOLOGY_SUITE_EXTRA_ARGS", "--filter cephadm")
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars["TEUTHOLOGY_SUITE_EXTRA_ARGS"]
        assert "--filter cephadm" in extra
        assert "--validate-sha1 false" in extra
        assert "--sha1 abc123" in extra
        assert f"--suite-dir {CONTAINER_CEPH_REPO_PATH}" in extra

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    def test_does_not_duplicate_flags(self, _mock_sha1, monkeypatch):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "image_builder": "package-build",
        }
        config["stacks"]["teuthology"]["local_artifacts"] = True
        monkeypatch.setenv(
            "TEUTHOLOGY_SUITE_EXTRA_ARGS",
            "--validate-sha1 false --sha1 abc123 --suite-sha1 abc123"
            f" --suite-dir {CONTAINER_CEPH_REPO_PATH}",
        )
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars["TEUTHOLOGY_SUITE_EXTRA_ARGS"]
        assert extra.count("--validate-sha1") == 1
        assert extra.count("--sha1") == 1
        assert extra.count("--suite-sha1") == 1
        assert extra.count("--suite-dir") == 1


class TestTeuthologyCephRepoMount:
    def test_create_cmd_includes_repo_volume_when_configured(self, tmp_path):
        config["containers"]["ceph_builder"] = {
            "repo": str(tmp_path / "ceph"),
            "image_builder": "package-build",
        }
        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        cmd = teuth.create_cmd
        volume_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        repo_mount = [v for v in volume_args if CONTAINER_CEPH_REPO_PATH in v]
        assert len(repo_mount) == 1
        assert f":{CONTAINER_CEPH_REPO_PATH}:ro" in repo_mount[0]

    def test_create_cmd_no_repo_volume_without_builder(self, tmp_path):
        config["containers"]["ceph_builder"].pop("repo", None)
        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        cmd = teuth.create_cmd
        volume_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-v"]
        repo_mount = [v for v in volume_args if CONTAINER_CEPH_REPO_PATH in v]
        assert len(repo_mount) == 0


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
    async def test_generates_merged_file(self, tmp_path):
        base = yaml.dump({"queue_host": "beanstalk", "queue_port": 11300})
        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = make_teuthology_cmd_mock(base)
        await teuth._generate_teuthology_yaml()
        assert teuth._generated_teuthology_yaml.exists()

    async def test_preserves_base_config(self, tmp_path):
        base = yaml.dump(
            {
                "queue_host": "beanstalk",
                "queue_port": 11300,
                "lock_server": "http://paddles:8080",
                "results_ui_server": "http://pulpito:8081/",
            }
        )
        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = make_teuthology_cmd_mock(base)
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert content["queue_host"] == "beanstalk"
        assert content["lock_server"] == "http://paddles:8080"
        assert content["results_ui_server"] == "http://pulpito:8081/"

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    @patch("ceph_devstack.resources.utils.git_branch", return_value="main")
    async def test_includes_local_artifact_defaults(
        self, _mock_branch, _mock_sha1, tmp_path
    ):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "build_distro": "centos9",
        }
        base = yaml.dump({"queue_host": "beanstalk"})
        teuth = Teuthology(
            data_dir=tmp_path,
            active_services=TEUTHOLOGY_SERVICES,
            local_artifacts=True,
        )
        teuth.cmd = make_teuthology_cmd_mock(base)
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        defaults = content["defaults"]
        assert defaults["install"]["repos"][0]["name"] == "local-ceph"
        assert defaults["install"]["repos"][0]["url"] == "http://package_repo:8080/rpm/"
        assert (
            defaults["cephadm"]["containers"]["image"]
            == "registry:5000/ceph:abc123-centos-stream9"
        )
        assert content["ceph_git_url"] == CONTAINER_CEPH_REPO_URL
        assert content["ceph_qa_suite_git_url"] == CONTAINER_CEPH_REPO_URL

    async def test_no_artifact_defaults_without_artifact_services(self, tmp_path):
        base = yaml.dump({"queue_host": "beanstalk"})
        services_no_artifacts = [
            s for s in TEUTHOLOGY_SERVICES if s not in ("registry", "package_repo")
        ]
        teuth = Teuthology(data_dir=tmp_path, active_services=services_no_artifacts)
        teuth.cmd = make_teuthology_cmd_mock(base)
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert content["queue_host"] == "beanstalk"
        assert "defaults" not in content

    async def test_skips_generation_on_extraction_failure(self, tmp_path):
        async def failing_cmd(args, check=False, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.collect_output = AsyncMock(return_value=("", "error"))
            return mock_proc

        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = failing_cmd
        await teuth._generate_teuthology_yaml()
        assert not teuth._generated_teuthology_yaml.exists()

    async def test_removes_stale_file_on_extraction_failure(self, tmp_path):
        stale = tmp_path / "teuthology.yaml"
        stale.write_text("old config")

        async def failing_cmd(args, check=False, **kwargs):
            mock_proc = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.collect_output = AsyncMock(return_value=("", ""))
            return mock_proc

        teuth = Teuthology(data_dir=tmp_path, active_services=TEUTHOLOGY_SERVICES)
        teuth.cmd = failing_cmd
        await teuth._generate_teuthology_yaml()
        assert not stale.exists()


class TestPublishedImagesDefault:
    """Feature-negative tests: with local_artifacts=false (default), no artifact
    overrides are injected even when ceph_builder.repo is set.

    Env-var wiring is covered by TestCrossStackIsolation below; this class
    tests the generated .teuthology.yaml side.
    """

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    @patch("ceph_devstack.resources.utils.git_branch", return_value="main")
    async def test_no_suite_verify_ceph_hash_override_without_local_artifacts(
        self, _mock_branch, _mock_sha1, tmp_path
    ):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "build_distro": "centos9",
        }
        base = yaml.dump({"queue_host": "beanstalk"})
        teuth = Teuthology(
            data_dir=tmp_path,
            active_services=TEUTHOLOGY_SERVICES,
            local_artifacts=False,
        )
        teuth.cmd = make_teuthology_cmd_mock(base)
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert "suite_verify_ceph_hash" not in content

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    @patch("ceph_devstack.resources.utils.git_branch", return_value="main")
    async def test_no_install_repos_override_without_local_artifacts(
        self, _mock_branch, _mock_sha1, tmp_path
    ):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "build_distro": "centos9",
        }
        base = yaml.dump({"queue_host": "beanstalk"})
        teuth = Teuthology(
            data_dir=tmp_path,
            active_services=TEUTHOLOGY_SERVICES,
            local_artifacts=False,
        )
        teuth.cmd = make_teuthology_cmd_mock(base)
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert "defaults" not in content

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    @patch("ceph_devstack.resources.utils.git_branch", return_value="main")
    async def test_ceph_git_url_still_set_without_local_artifacts(
        self, _mock_branch, _mock_sha1, tmp_path
    ):
        """Repo mount optimization: ceph_git_url points at mounted repo even
        without local_artifacts, so teuthology workers avoid network clones."""
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "build_distro": "centos9",
        }
        base = yaml.dump({"queue_host": "beanstalk"})
        teuth = Teuthology(
            data_dir=tmp_path,
            active_services=TEUTHOLOGY_SERVICES,
            local_artifacts=False,
        )
        teuth.cmd = make_teuthology_cmd_mock(base)
        await teuth._generate_teuthology_yaml()
        content = yaml.safe_load(teuth._generated_teuthology_yaml.read_text())
        assert content["ceph_git_url"] == CONTAINER_CEPH_REPO_URL
        assert content["ceph_qa_suite_git_url"] == CONTAINER_CEPH_REPO_URL


class TestCrossStackIsolation:
    """ceph_builder.repo configured globally does not inject artifacts
    into the teuthology stack when local_artifacts is false."""

    @patch("ceph_devstack.resources.utils.git_sha1", return_value="abc123")
    def test_builder_repo_does_not_contaminate_teuthology_default(self, _mock_sha1):
        config["containers"]["ceph_builder"] = {
            "repo": "/tmp/fake-ceph",
            "build_distro": "centos9",
            "image_builder": "package-build",
        }
        # local_artifacts defaults to false in config.toml
        config["stacks"]["teuthology"].pop("local_artifacts", None)
        devstack = CephDevStack()
        teuth = devstack.service_specs["teuthology"]["objects"][0]
        extra = teuth.env_vars.get("TEUTHOLOGY_SUITE_EXTRA_ARGS", "")
        assert "--sha1" not in extra
        assert "--validate-sha1" not in extra
        assert teuth.env_vars.get("TEUTHOLOGY_CEPH_REPO", "") == ""
        assert teuth.env_vars.get("TEUTHOLOGY_SUITE_REPO", "") == ""
        # But suite-dir IS set (repo mount for discovery)
        assert f"--suite-dir {CONTAINER_CEPH_REPO_PATH}" in extra
