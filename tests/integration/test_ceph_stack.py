"""Ceph stack integration test (podman + live cluster)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = Path(__file__).with_name("test_ceph_stack.sh")

pytestmark = pytest.mark.skipif(
    os.environ.get("CEPH_DEVSTACK_INTEGRATION") != "1"
    or shutil.which("podman") is None,
    reason="requires CEPH_DEVSTACK_INTEGRATION=1 and podman",
)


@pytest.mark.integration
def test_ceph_stack_reaches_health_ok_and_dashboard() -> None:
    subprocess.run(["bash", str(_SCRIPT)], check=True, cwd=_REPO_ROOT)
