import asyncio
import pytest
from packaging.version import parse as parse_version
from unittest.mock import AsyncMock, patch


from ceph_devstack import config, requirements


@pytest.fixture(scope="class")
def cls():
    return requirements.Requirement


@pytest.fixture(scope="class")
def req(cls):
    return cls()


class TestRequirement:
    @pytest.fixture(scope="class")
    def cls(self):
        class TestReq(requirements.Requirement):
            check_cmd = ["test", "command"]

        return TestReq

    async def test_check_returns_true_on_zero_rc(self, req):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=0)
        with patch.object(req.host, "arun", return_value=mock_proc):
            result = await req.check()
            assert result is True

    async def test_check_returns_false_on_nonzero_rc(self, req):
        mock_proc = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)
        with patch.object(req.host, "arun", return_value=mock_proc):
            result = await req.check()
            assert result is False

    async def test_evaluate_delegates_to_check(self, req):
        with patch.object(req, "check", return_value=True) as mock_check:
            result = await req.evaluate()
            mock_check.assert_called_once()
            assert result is True


class TestFixableRequirement:
    @pytest.fixture(scope="class")
    def cls(self):
        class TestReq(requirements.FixableRequirement):
            check_cmd = ["test", "-f", "/tmp/testfile"]
            fix_cmd = ["touch", "/tmp/testfile"]
            suggest_msg = "Test file missing"

        return TestReq

    async def test_evaluate_returns_true_when_check_passes(self, req):
        with patch.object(req, "check", return_value=True):
            result = await req.evaluate()
            assert result is True

    async def test_evaluate_returns_false_when_check_fails(self, req):
        config.setdefault("args", {})
        config["args"]["fix"] = False
        with (
            patch.object(req, "check", return_value=False),
            patch.object(req, "suggest") as mock_suggest,
        ):
            result = await req.evaluate()
            assert result is False
            mock_suggest.assert_called_once()

    async def test_evaluate_calls_fix_when_fix_flag_set(self, req):
        config.setdefault("args", {})
        config["args"]["fix"] = True
        with (
            patch.object(req, "check", return_value=False),
            patch.object(req, "fix", return_value=True) as mock_fix,
        ):
            result = await req.evaluate()
            assert result is True
            mock_fix.assert_called_once()

    async def test_evaluate_returns_false_when_fix_fails(self, req):
        config.setdefault("args", {})
        config["args"]["fix"] = True
        with (
            patch.object(req, "check", return_value=False),
            patch.object(req, "fix", return_value=False) as mock_fix,
        ):
            result = await req.evaluate()
            assert result is False
            mock_fix.assert_called_once()

    async def test_fix_requires_fix_cmd(self, req):
        req.fix_cmd = []
        with pytest.raises(AssertionError):
            await req.fix()
            asyncio.run(req.fix())


class TestLocalRequirement:
    @pytest.fixture(scope="class")
    def cls(self):
        class TestReq(requirements.LocalRequirement):
            check_cmd = ["test"]

        return TestReq

    def test_local_requirement_uses_local_host(self, req):
        assert req.host == requirements.local_host


class TestPodmanVersionInit:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.PodmanVersion

    @pytest.fixture(scope="class")
    def req(self, cls):
        return cls("4.0.0")

    def test_podman_version_init_sets_version(self, req):
        assert req.required_version == parse_version("4.0.0")

    def test_podman_version_init_sets_msg(self, cls):
        req = cls("4.0.0", "Custom message")
        assert req.msg == "Custom message"


class TestSysctlValueInit:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.SysctlValue

    @pytest.fixture(scope="class")
    def req(self, cls):
        return cls("fs.aio-max-nr", 2097152)

    def test_sysctl_value_init_sets_key(self, req):
        assert req.key == "fs.aio-max-nr"

    def test_sysctl_value_init_sets_min_value(self, req):
        assert req.min_value == 2097152

    def test_sysctl_value_init_fix_cmd(self, req):
        assert req.fix_cmd == ["sudo", "sysctl", "fs.aio-max-nr=2097152"]


class TestSELinuxBooleanInit:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.SELinuxBoolean

    @pytest.fixture(scope="class")
    def req(self, cls):
        return cls("test_bool")

    def test_selinux_boolean_init_sets_boolean_name(self, req):
        assert req.boolean_name == "test_bool"

    def test_selinux_boolean_init_fix_cmd(self, req):
        assert req.fix_cmd == ["sudo", "setsebool", "-P", "test_bool=true"]

    def test_selinux_boolean_init_suggest_msg(self, req):
        assert "test_bool" in req.suggest_msg


class TestFuseOverlayfsPresence:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.FuseOverlayfsPresence

    def test_fuse_overlayfs_presence_check_cmd(self, req):
        assert req.check_cmd == ["command", "-v", "fuse-overlayfs"]

    def test_fuse_overlayfs_presence_fix_cmd(self, req):
        assert req.fix_cmd == ["sudo", "dnf", "install", "-y", "fuse-overlayfs"]

    def test_fuse_overlayfs_presence_suggest_msg(self, req):
        assert req.suggest_msg == "Could not find fuse-overlayfs"


class TestCgroupV2Properties:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.CgroupV2

    def test_cgroup_v2_suggest_msg(self, req):
        assert req.suggest_msg == "cgroup v2 is not enabled"

    def test_cgroup_v2_fix_cmd(self, req):
        assert req.fix_cmd == [
            "sudo",
            "grubby",
            "--update-kernel=ALL",
            "--args='systemd.unified_cgroup_hierarchy=1'",
        ]


class TestCgroupV2Check:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.CgroupV2

    async def test_cgroup_v2_check_true(self, req):
        mock_info = {"host": {"cgroupVersion": "v2"}}
        with patch.object(req.host, "podman_info", return_value=mock_info):
            result = await req.check()
            assert result is True

    async def test_cgroup_v2_check_false(self, req):
        mock_info = {"host": {"cgroupVersion": "v1"}}
        with patch.object(req.host, "podman_info", return_value=mock_info):
            result = await req.check()
            assert result is False


class TestPodmanDNSPluginInit:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.PodmanDNSPlugin

    @pytest.fixture(scope="class", params=["centos", "ubuntu", "debian"])
    def os_type(self, request):
        return request.param

    @pytest.fixture(scope="class")
    def dns_plugin_path(self, os_type):
        if os_type == "centos":
            return "/usr/libexec/cni/dnsname"
        elif os_type in ["ubuntu", "debian"]:
            return "/usr/lib/cni/dnsname"

    def test_podman_dns_plugin_config(self, cls, os_type, dns_plugin_path):
        with patch.object(cls.host, "os_type", return_value=os_type):
            req = cls()
            assert req.check_cmd == ["test", "-x", dns_plugin_path]


class TestAppArmorProfile:
    @pytest.fixture(scope="class")
    def cls(self):
        return requirements.AppArmorProfile

    def test_apparmor_profile_check_cmd(self, req):
        assert req.check_cmd == ["test", "-f", "/etc/apparmor.d/local/unix-chkpwd"]

    def test_apparmor_profile_fix_cmd(self, req):
        assert req.fix_cmd[-1].endswith("&& systemctl reload apparmor")

    def test_apparmor_profile_suggest_msg(self, req):
        assert req.suggest_msg == "Did not find required apparmor profile"


class TestFixableRequirementSuggestMsg:
    @pytest.fixture(scope="class")
    def cls(self):
        class TestReq(requirements.FixableRequirement):
            check_cmd = ["test"]
            fix_cmd = ["fix"]
            suggest_msg = "Please fix this"

        return TestReq

    def test_fixable_requirement_has_suggest_msg(self, req):
        assert req.suggest_msg == "Please fix this"


class TestCheckRequirements:
    async def test_check_requirements_returns_false_when_podman_not_platform(self):
        with patch("ceph_devstack.requirements.PodmanPlatform") as MockPlatform:
            mock_platform = AsyncMock()
            mock_platform.evaluate = AsyncMock(return_value=False)
            MockPlatform.return_value = mock_platform
            result = await requirements.check_requirements()
            assert result is False
            mock_platform.evaluate.assert_called_once()

    async def test_check_requirements_returns_false_on_overlay_failure(self):
        with (
            patch("ceph_devstack.requirements.PodmanPlatform") as MockPlatform,
            patch("ceph_devstack.requirements.PodmanGraphDriver") as MockGraph,
        ):
            mock_platform = AsyncMock()
            mock_platform.evaluate = AsyncMock(return_value=True)
            MockPlatform.return_value = mock_platform

            mock_graph = AsyncMock()
            mock_graph.evaluate = AsyncMock(return_value=False)
            MockGraph.return_value = mock_graph

            result = await requirements.check_requirements()
            assert result is False

    async def test_check_requirements_returns_true_when_all_pass(self):
        with (
            patch("ceph_devstack.requirements.PodmanPlatform") as MockPlatform,
            patch("ceph_devstack.requirements.PodmanGraphDriver") as MockGraph,
            patch("ceph_devstack.requirements.PodmanVersion") as MockVersion,
            patch("ceph_devstack.requirements.KernelVersionForOverlay") as MockKernel,
            patch("ceph_devstack.requirements.CgroupV2") as MockCgroup,
            patch(
                "ceph_devstack.requirements.KernelVersionForCgroupV2"
            ) as MockKernelCgroup,
            patch("ceph_devstack.requirements.PodmanRuntime") as MockRuntime,
            patch("ceph_devstack.requirements.host.selinux_enforcing") as mock_selinux,
            patch("ceph_devstack.requirements.SysctlValue") as MockSysctl,
        ):
            mock_platform = AsyncMock()
            mock_platform.evaluate = AsyncMock(return_value=True)
            MockPlatform.return_value = mock_platform

            mock_graph = AsyncMock()
            mock_graph.evaluate = AsyncMock(return_value=True)
            MockGraph.return_value = mock_graph

            mock_version = AsyncMock()
            mock_version.evaluate = AsyncMock(return_value=True)
            MockVersion.return_value = mock_version

            mock_kernel = AsyncMock()
            mock_kernel.evaluate = AsyncMock(return_value=True)
            MockKernel.return_value = mock_kernel

            mock_cgroup = AsyncMock()
            mock_cgroup.evaluate = AsyncMock(return_value=True)
            MockCgroup.return_value = mock_cgroup

            mock_kernel_cgroup = AsyncMock()
            mock_kernel_cgroup.evaluate = AsyncMock(return_value=True)
            MockKernelCgroup.return_value = mock_kernel_cgroup

            mock_runtime = AsyncMock()
            mock_runtime.evaluate = AsyncMock(return_value=True)
            MockRuntime.return_value = mock_runtime

            mock_selinux.return_value = False

            mock_sysctl = AsyncMock()
            mock_sysctl.evaluate = AsyncMock(return_value=True)
            MockSysctl.return_value = mock_sysctl

            result = await requirements.check_requirements()
            assert result is True

    async def test_check_requirements_returns_false_on_runtime_failure(self):
        with (
            patch("ceph_devstack.requirements.PodmanPlatform") as MockPlatform,
            patch("ceph_devstack.requirements.PodmanGraphDriver") as MockGraph,
            patch("ceph_devstack.requirements.PodmanVersion") as MockVersion,
            patch("ceph_devstack.requirements.KernelVersionForOverlay") as MockKernel,
            patch("ceph_devstack.requirements.CgroupV2") as MockCgroup,
            patch(
                "ceph_devstack.requirements.KernelVersionForCgroupV2"
            ) as MockKernelCgroup,
            patch("ceph_devstack.requirements.PodmanRuntime") as MockRuntime,
            patch("ceph_devstack.requirements.host.selinux_enforcing") as mock_selinux,
        ):
            mock_platform = AsyncMock()
            mock_platform.evaluate = AsyncMock(return_value=True)
            MockPlatform.return_value = mock_platform

            mock_graph = AsyncMock()
            mock_graph.evaluate = AsyncMock(return_value=True)
            MockGraph.return_value = mock_graph

            mock_version = AsyncMock()
            mock_version.evaluate = AsyncMock(return_value=True)
            MockVersion.return_value = mock_version

            mock_kernel = AsyncMock()
            mock_kernel.evaluate = AsyncMock(return_value=True)
            MockKernel.return_value = mock_kernel

            mock_cgroup = AsyncMock()
            mock_cgroup.evaluate = AsyncMock(return_value=True)
            MockCgroup.return_value = mock_cgroup

            mock_kernel_cgroup = AsyncMock()
            mock_kernel_cgroup.evaluate = AsyncMock(return_value=True)
            MockKernelCgroup.return_value = mock_kernel_cgroup

            mock_runtime = AsyncMock()
            mock_runtime.evaluate = AsyncMock(return_value=False)
            MockRuntime.return_value = mock_runtime

            mock_selinux.return_value = False

            result = await requirements.check_requirements()
            assert result is False

    async def test_check_requirements_returns_false_on_selinux_bool_failure(self):
        with (
            patch("ceph_devstack.requirements.PodmanPlatform") as MockPlatform,
            patch("ceph_devstack.requirements.PodmanGraphDriver") as MockGraph,
            patch("ceph_devstack.requirements.KernelVersionForOverlay") as MockKernel,
            patch("ceph_devstack.requirements.CgroupV2") as MockCgroup,
            patch(
                "ceph_devstack.requirements.KernelVersionForCgroupV2"
            ) as MockKernelCgroup,
            patch("ceph_devstack.requirements.PodmanRuntime") as MockRuntime,
            patch("ceph_devstack.requirements.host.selinux_enforcing") as mock_selinux,
            patch("ceph_devstack.requirements.SELinuxBoolean") as MockSELinuxBoolean,
        ):
            mock_platform = AsyncMock()
            mock_platform.evaluate = AsyncMock(return_value=True)
            MockPlatform.return_value = mock_platform

            mock_graph = AsyncMock()
            mock_graph.evaluate = AsyncMock(return_value=True)
            MockGraph.return_value = mock_graph

            with patch("ceph_devstack.requirements.PodmanVersion") as MockVersion:
                mock_version = AsyncMock()
                mock_version.evaluate = AsyncMock(return_value=True)
                MockVersion.return_value = mock_version

                mock_kernel = AsyncMock()
                mock_kernel.evaluate = AsyncMock(return_value=True)
                MockKernel.return_value = mock_kernel

                mock_cgroup = AsyncMock()
                mock_cgroup.evaluate = AsyncMock(return_value=True)
                MockCgroup.return_value = mock_cgroup

                mock_kernel_cgroup = AsyncMock()
                mock_kernel_cgroup.evaluate = AsyncMock(return_value=True)
                MockKernelCgroup.return_value = mock_kernel_cgroup

                mock_runtime = AsyncMock()
                mock_runtime.evaluate = AsyncMock(return_value=True)
                MockRuntime.return_value = mock_runtime

                mock_selinux.return_value = True

                mock_sel = AsyncMock()
                mock_sel.evaluate = AsyncMock(return_value=False)
                MockSELinuxBoolean.return_value = mock_sel

                result = await requirements.check_requirements()
                assert result is False

    async def test_check_requirements_returns_false_on_sysctl_failure(self):
        with (
            patch("ceph_devstack.requirements.PodmanPlatform") as MockPlatform,
            patch("ceph_devstack.requirements.PodmanGraphDriver") as MockGraph,
            patch("ceph_devstack.requirements.PodmanVersion") as MockVersion,
            patch("ceph_devstack.requirements.KernelVersionForOverlay") as MockKernel,
            patch("ceph_devstack.requirements.CgroupV2") as MockCgroup,
            patch(
                "ceph_devstack.requirements.KernelVersionForCgroupV2"
            ) as MockKernelCgroup,
            patch("ceph_devstack.requirements.PodmanRuntime") as MockRuntime,
            patch("ceph_devstack.requirements.host.selinux_enforcing") as mock_selinux,
            patch("ceph_devstack.requirements.SysctlValue") as MockSysctl,
        ):
            mock_platform = AsyncMock()
            mock_platform.evaluate = AsyncMock(return_value=True)
            MockPlatform.return_value = mock_platform

            mock_graph = AsyncMock()
            mock_graph.evaluate = AsyncMock(return_value=True)
            MockGraph.return_value = mock_graph

            mock_version = AsyncMock()
            mock_version.evaluate = AsyncMock(return_value=True)
            MockVersion.return_value = mock_version

            mock_kernel = AsyncMock()
            mock_kernel.evaluate = AsyncMock(return_value=True)
            MockKernel.return_value = mock_kernel

            mock_cgroup = AsyncMock()
            mock_cgroup.evaluate = AsyncMock(return_value=True)
            MockCgroup.return_value = mock_cgroup

            mock_kernel_cgroup = AsyncMock()
            mock_kernel_cgroup.evaluate = AsyncMock(return_value=True)
            MockKernelCgroup.return_value = mock_kernel_cgroup

            mock_runtime = AsyncMock()
            mock_runtime.evaluate = AsyncMock(return_value=True)
            MockRuntime.return_value = mock_runtime

            mock_selinux.return_value = False

            mock_sysctl = AsyncMock()
            mock_sysctl.evaluate = AsyncMock(return_value=False)
            MockSysctl.return_value = mock_sysctl

            result = await requirements.check_requirements()
            assert result is False
