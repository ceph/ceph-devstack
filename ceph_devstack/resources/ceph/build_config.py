"""Shared build-related config properties for CephBuilder and CephNode."""

from pathlib import Path
from typing import Any, Dict


class CephBuildConfigMixin:
    """Mixin providing repo, image_builder, build_subdir, and build_path.

    Requires ``self.config`` (a dict-like) from ``PodmanResource``.
    """

    config: Dict[str, Any]

    @property
    def repo(self) -> Path | None:
        r = self.config.get("repo", "")
        if not r:
            return None
        return Path(r).expanduser().resolve()

    @property
    def image_builder(self) -> str:
        return self.config["image_builder"]

    @property
    def build_subdir(self) -> str:
        build_dir = self.config.get("build_dir", "build")
        if not build_dir:
            return "build"
        return str(build_dir).strip("/")

    @property
    def build_path(self) -> Path:
        if not self.repo:
            return Path()
        return self.repo / self.build_subdir
