import hashlib
import re
import shlex

from subprocess import CalledProcessError

from ceph_devstack import logger, PROJECT_ROOT
from ceph_devstack.block_pool import BlockPool, BlockPoolError, shared_pool
from ceph_devstack.host import Host
from ceph_devstack.requirements import Requirement, FixableRequirement


def _host_user(host: Host) -> str:
    return host.output(["whoami"]).stdout.decode().strip()


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
            user = _host_user(self.host)
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


class BlockPoolRequirement(Requirement):
    """Passes without a configured pool; fails (logged) if its config is invalid.

    An invalid config fails in evaluate(), before any suggest or --fix step,
    since there is nothing to fix on the host for it.
    """

    async def evaluate(self) -> bool:
        try:
            pool = shared_pool()
        except BlockPoolError as e:
            logger.error(f"block pool: {e}")
            return False
        return pool is None or await super().evaluate()

    async def check(self) -> bool:
        pool = shared_pool()
        return pool is None or await self.check_pool(pool)

    async def check_pool(self, pool: BlockPool) -> bool:
        raise NotImplementedError


# Fedora CoreOS (e.g. a podman machine) defines the disk group in
# /usr/lib/group, where usermod silently fails to add members; copy the group
# into /etc/group first. Elsewhere it is already there and this does nothing.
DISK_GROUP_TO_ETC = (
    "grep -q '^disk:' /etc/group || grep '^disk:' /usr/lib/group >> /etc/group"
)


class BlockPoolDiskGroup(BlockPoolRequirement, FixableRequirement):
    check_cmd = ["bash", "-c", "id -nG | grep -qw disk"]
    suggest_msg = (
        "block pool parent requires membership in the disk group "
        "(log out and back in after joining)"
    )

    async def check_pool(self, pool: BlockPool) -> bool:
        if await Requirement.check(self):
            return True
        user = shlex.quote(_host_user(self.host))
        self.fix_cmd = [
            "sudo",
            "bash",
            "-c",
            f"{DISK_GROUP_TO_ETC}; usermod -a -G disk {user}",
        ]
        return False


class BlockPoolParentAccessible(BlockPoolRequirement):
    suggest_msg = "block pool parent must be readable and writable by this user"

    async def check_pool(self, pool: BlockPool) -> bool:
        proc = await self.host.arun(
            ["test", "-r", pool.parent, "-a", "-w", pool.parent]
        )
        if await proc.wait() == 0:
            return True
        logger.error(
            f"{self.suggest_msg} ({pool.parent}). "
            "Join the disk group and re-login, or fix device permissions."
        )
        return False


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
