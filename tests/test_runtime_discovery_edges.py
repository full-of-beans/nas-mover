from pathlib import Path
from types import SimpleNamespace

import pytest

from nas_mover.discovery import discover_runtime_pool


def _runner_with_branches(branches: str):
    def runner(command, **kwargs):
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0, stdout="")
        if command[0] == "getfattr":
            return SimpleNamespace(returncode=0, stdout=branches)
        if command[3] == "FSTYPE":
            return SimpleNamespace(returncode=0, stdout="fuse.mergerfs\n")
        if command[3] == "OPTIONS":
            return SimpleNamespace(returncode=0, stdout="rw,minfreespace=20G\n")
        raise AssertionError(command)

    return runner


def test_runtime_pool_rejects_empty_branch_list(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="empty runtime branch list"):
        discover_runtime_pool(tmp_path / "pool", _runner_with_branches("\n"))


def test_runtime_pool_rejects_branch_without_mode_separator(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Invalid mergerfs runtime branch entry"):
        discover_runtime_pool(tmp_path / "pool", _runner_with_branches("/ssd1\n"))


def test_runtime_pool_rejects_duplicate_branches(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="duplicate runtime branches"):
        discover_runtime_pool(
            tmp_path / "pool",
            _runner_with_branches("/ssd1=RW:/ssd1=RW\n"),
        )
