import hashlib
import re

from subprocess import CalledProcessError

from ceph_devstack import logger, PROJECT_ROOT
from ceph_devstack.requirements import Requirement, FixableRequirement


class HasSudo(Requirement):
    check_cmd = ["sudo", "true"]
    suggest_msg = "sudo access is required"


class LoopControlDeviceExists(FixableRequirement):
    device = "/dev/loop-control"
    check_cmd = ["test", "-e", device]
    suggest_msg = f"{device} does not exist"
    fix_cmd = ["sudo", "modprobe", "loop"]


class LoopControlDeviceWriteable(FixableRequirement):
    device = "/dev/loop-control"
    check_cmd = ["test", "-w", device]
    suggest_msg = f"Cannot write to {device}"

    async def check(self):
        if not (result := await super().check()):
            group = (
                self.host.run(["stat", "--printf", "%G", self.device])
                .communicate()[0]
                .decode()
            )
            user = self.host.run(["whoami"]).communicate()[0].strip().decode()
            if self.host.type == "local":
                self.fix_cmd = ["sudo", "usermod", "-a", "-G", group, user]
            else:
                self.fix_cmd = ["sudo", "chgrp", user, self.device]
            self.suggest_msg = f"Cannot write to {self.device}"
        return result

    async def suggest(self):
        await super().suggest()
        if self.host.type == "local":
            logger.warning(
                "Note that group modifications require a logout to take effect."
            )


class SELinuxModule(FixableRequirement):
    MODULE_NAME = "ceph_devstack"
    SHIPPED_PP = PROJECT_ROOT / f"{MODULE_NAME}.pp"

    def __init__(self):
        fix_cmd = self.fix_cmd_prebuilt
        if self.host.type == "remote":
            fix_cmd = ["podman", "machine", "ssh", "--"] + fix_cmd
        self.fix_cmd = fix_cmd

    fix_cmd_prebuilt = ["sudo", "semodule", "-i", str(SHIPPED_PP)]
    suggest_msg = (
        "SELinux is in Enforcing mode. To run nested rootless podman "
        "containers, it is necessary to install ceph-devstack's SELinux "
        "module (module may be missing or outdated)"
    )

    async def check(self):
        proc = await self.host.arun(["sudo", "semodule", "-l"])
        assert proc.stdout is not None
        await proc.wait()
        out = (await proc.stdout.read()).decode()
        if self.MODULE_NAME not in out.split("\n"):
            return False
        if not self.SHIPPED_PP.exists():
            logger.warning(
                f"Shipped SELinux module not found at {self.SHIPPED_PP}; "
                "skipping hash check"
            )
            return True
        installed_hash = await self._installed_module_hash()
        if installed_hash is None:
            logger.warning(
                "Could not extract the installed SELinux module; "
                "falling back to presence-only check"
            )
            return True
        shipped_hash = hashlib.sha256(self.SHIPPED_PP.read_bytes()).hexdigest()
        if installed_hash != shipped_hash:
            logger.warning(
                f"SELinux module hash mismatch: shipped={shipped_hash}, "
                f"installed={installed_hash}"
            )
            return False
        return True

    async def _installed_module_hash(self) -> str | None:
        """sha256 of the installed module, or None if it can't be determined."""
        # semodule -E writes a byte-identical .pp into the cwd (and chatters
        # on stdout, hence the redirect); always clean up the temp dir.
        script = (
            "d=$(mktemp -d) || exit 1; "
            "trap 'sudo rm -rf \"$d\"' EXIT; "
            'cd "$d" && '
            f"sudo semodule -E {self.MODULE_NAME} >/dev/null 2>&1 && "
            f"sha256sum {self.MODULE_NAME}.pp"
        )
        try:
            proc = await self.host.arun(["bash", "-c", script])
            assert proc.stdout is not None
            if await proc.wait() != 0:
                return None
            out = (await proc.stdout.read()).decode().split()
        except (OSError, CalledProcessError):
            return None
        # Output is "hash  filename"; anything else is unparseable.
        if out and re.fullmatch(r"[0-9a-f]{64}", out[0]):
            return out[0]
        return None
