import sys

from pathlib import Path
from packaging.version import parse as parse_version, Version
from typing import List

from ceph_devstack import config, logger
from ceph_devstack.host import Host, host, local_host


class Requirement:
    host: Host = host
    check_cmd: List[str]

    async def evaluate(self) -> bool:
        return await self.check()

    async def check(self) -> bool:
        proc = await self.host.arun(self.check_cmd)
        return await proc.wait() == 0


class FixableRequirement(Requirement):
    fix_cmd: List[str]
    suggest_msg: str

    async def evaluate(self) -> bool:
        if await self.check() is True:
            return True
        if config["args"].get("fix", False):
            return await self.fix()
        else:
            await self.suggest()
            return False

    async def suggest(self):
        if hasattr(self, "suggest_msg"):
            logger.error(f"{self.suggest_msg}. Try: {' '.join(self.fix_cmd)}")

    async def fix(self) -> bool:
        assert self.fix_cmd, "Attempted to fix without a fix command"
        proc = await self.host.arun(self.fix_cmd)
        return await proc.wait() == 0


class LocalRequirement(Requirement):
    host = local_host


class PodmanPlatform(Requirement):
    async def check(self):
        result = False
        try:
            podman_info = await self.host.podman_info()
        except FileNotFoundError:
            logger.error("podman not found. Try: dnf install podman")
            return False
        try:
            host_os = (
                podman_info["host"].get("Os") or podman_info["host"]["os"]
            ).lower()
            if host_os == "linux":
                result = True
        except KeyError:
            host_os = sys.platform.lower()
            result = False
            if sys.platform == "darwin":
                logger.error(
                    "The podman machine (VM) is not running. "
                    "Try: podman machine init --now"
                )
            else:
                logger.error(
                    "Unknown error trying to query podman. Is podman installed?"
                )
            return result
        if host_os != "linux":
            logger.error("The platform '{host_os}' is not currently supported.")
        return result


class PodmanGraphDriver(Requirement):
    async def check(self):
        podman_info = await self.host.podman_info()
        storage_conf_path = podman_info["store"]["configFile"]
        graph_driver = podman_info["store"]["graphDriverName"]
        if graph_driver == "overlay":
            return True
        else:
            self.suggest_msg = (
                f"The configured graph driver is '{graph_driver}'. "
                f"It must be set to 'overlay' in {storage_conf_path}."
            )
            return False


class KernelVersionForOverlay(Requirement):
    async def check(self):
        kernel_version = self.host.kernel_version()
        version_for_overlay = Version("5.12")
        if kernel_version < version_for_overlay:
            self.suggest_msg = (
                f"Kernel version ({kernel_version}) is too old to support native rootless "
                f"overlayfs (needs {version_for_overlay})"
            )
            return False
        return True


class KernelVersionForCgroupV2(Requirement):
    async def check(self):
        version_for_cgroup = Version("4.15")
        kernel_version = self.host.kernel_version()
        if not kernel_version >= version_for_cgroup:
            self.suggest_msg = (
                f"Kernel version ({kernel_version}) is too old to support cgroup v2 "
                f"(needs {version_for_cgroup})"
            )
        return False


class CgroupV2(FixableRequirement):
    suggest_msg = "cgroup v2 is not enabled"
    fix_cmd = [
        "sudo",
        "grubby",
        "--update-kernel=ALL",
        "--args='systemd.unified_cgroup_hierarchy=1'",
    ]

    async def check(self):
        podman_info = await self.host.podman_info()
        return podman_info["host"]["cgroupVersion"] == "v2"


class PodmanVersion(Requirement):
    def __init__(self, version: str, msg: str = ""):
        self.required_version = parse_version(version)
        self.msg = msg

    async def check(self):
        podman_info = await self.host.podman_info()
        podman_version = parse_version(podman_info["version"]["Version"])
        if podman_version < self.required_version:
            if self.msg:
                logger.warning(self.msg)
            return False
        return True


class PodmanRuntime(Requirement):
    async def check(self):
        podman_info = await self.host.podman_info()
        storage_conf_path = podman_info["store"]["configFile"]
        runtime = podman_info["host"]["ociRuntime"]["name"]
        if runtime == "crun":
            return True
        else:
            containers_conf_path = Path(storage_conf_path).parent / "containers.conf"
            cmd = host.cmd(["podman", "system", "reset"])
            logger.error(
                f"The configured runtime is '{runtime}'. "
                f"It must be set to 'crun' in {containers_conf_path}. "
                f"Afterward, run '{cmd}'."
            )
            return False


class SELinuxBoolean(FixableRequirement):
    def __init__(self, boolean_name: str):
        super().__init__()
        self.boolean_name = boolean_name
        self.fix_cmd = ["sudo", "setsebool", "-P", f"{self.boolean_name}=true"]
        self.suggest_msg = f"SELinux boolean '{self.boolean_name}' must be enabled"

    async def check(self):
        return await self.host.check_selinux_bool(self.boolean_name)


class SysctlValue(FixableRequirement):
    def __init__(self, name: str, min_value: int):
        super().__init__()
        self.key = name
        self.min_value = min_value
        self.fix_cmd = ["sudo", "sysctl", f"{name}={min_value}"]

    async def check(self):
        current_value = await self.host.get_sysctl_value(self.key)
        self.suggest_msg = f"sysctl setting {self.key} ({current_value}) is too low"
        return current_value >= self.min_value


class PodmanDNSPlugin(FixableRequirement):
    suggest_msg = "Could not find the podman DNS plugin"

    def __init__(self):
        os_type = self.host.os_type()
        if os_type == "centos":
            dns_plugin_path = "/usr/libexec/cni/dnsname"
            self.check_cmd = ["test", "-x", dns_plugin_path]
            self.fix_cmd = ["sudo", "dnf", "install", "-y", dns_plugin_path]
        elif os_type in ["ubuntu", "debian"]:
            dns_plugin_path = "/usr/lib/cni/dnsname"
            self.check_cmd = ["test", "-x", dns_plugin_path]
            self.fix_cmd = [
                "sudo",
                "apt",
                "install",
                "-y",
                "golang-github-containernetworking-plugin-dnsname",
            ]


class FuseOverlayfsPresence(FixableRequirement):
    check_cmd = ["command", "-v", "fuse-overlayfs"]
    suggest_msg = "Could not find fuse-overlayfs"
    fix_cmd = ["sudo", "dnf", "install", "-y", "fuse-overlayfs"]


class AppArmorProfile(FixableRequirement):
    _profile_path = "/etc/apparmor.d/local/unix-chkpwd"
    _profile_content = '"capability dac_override,"'
    check_cmd = ["test", "-f", _profile_path]
    suggest_msg = "Did not find required apparmor profile"
    fix_cmd = [
        "sudo",
        "bash",
        "-c",
        f"echo -e {_profile_content} > {_profile_path} && systemctl reload apparmor",
    ]


async def check_requirements():
    if not await PodmanPlatform().evaluate():
        return False

    result = True
    # kernel and podman versions for native overlay filesystem
    result = result and await PodmanGraphDriver().evaluate()
    podman_overlay_version = "3.10"
    podman_version_overlay = await PodmanVersion(
        podman_overlay_version,
        "Podman version is too old for rootless native overlayfs (needs {podman_overlay_version})",
    ).evaluate()
    needs_fuse = not (
        await KernelVersionForOverlay().evaluate() and podman_version_overlay
    )
    # if not using native overlay, we need fuse-overlayfs
    if needs_fuse:
        result = result and await FuseOverlayfsPresence().evaluate()

    # cgroup v2
    if not await CgroupV2().evaluate():
        result = result and await KernelVersionForCgroupV2().evaluate()

    # runtime
    result = result and await PodmanRuntime().evaluate()

    # SELinux
    if await host.selinux_enforcing():
        result = result and await SELinuxBoolean("container_manage_cgroup").evaluate()
        result = result and await SELinuxBoolean("container_use_devices").evaluate()

    # AppArmor
    if await host.apparmor_enabled():
        result = result and await AppArmorProfile().evaluate()

    # podman DNS plugin
    if not await PodmanVersion("5.0").evaluate():
        result = result and await PodmanDNSPlugin().evaluate()

    # sysctl settings for OSD
    result = result and await SysctlValue("fs.aio-max-nr", 2097152).evaluate()
    result = result and await SysctlValue("kernel.pid_max", 4194304).evaluate()

    return result
