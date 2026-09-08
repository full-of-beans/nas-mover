from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import parse_size
from .models import Branch


@dataclass(frozen=True)
class Pool:
    mountpoint: Path
    branches: list[Path]
    options: dict[str, str | bool]
    min_free_bytes: int


def _parse_options(raw_options: str) -> dict[str, str | bool]:
    options: dict[str, str | bool] = {}
    for option in raw_options.split(","):
        key, separator, value = option.partition("=")
        options[key] = value if separator else True
    return options


def parse_fstab(path: Path, mount_override: Path | None = None) -> Pool:
    entries: list[tuple[str, str, str]] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            continue
        if len(fields) >= 4 and fields[2] == "fuse.mergerfs":
            entries.append((fields[0], fields[1], fields[3]))
    if mount_override is not None:
        entries = [entry for entry in entries if Path(entry[1]) == mount_override]
    if not entries:
        raise RuntimeError("No matching fuse.mergerfs entry found in fstab")
    if len(entries) != 1:
        raise RuntimeError("More than one matching mergerfs entry exists")
    source, mountpoint, raw_options = entries[0]
    options = _parse_options(raw_options)
    return Pool(
        Path(mountpoint),
        [Path(branch) for branch in source.split(":")],
        options,
        parse_size(str(options["minfreespace"])) if "minfreespace" in options else 0,
    )


def discover_runtime_pool(
    mountpoint: Path,
    runner=subprocess.run,
    runtime_attribute: str = "user.mergerfs.branches",
) -> Pool:
    """Discover the active mergerfs topology from the mounted filesystem.

    mergerfs branches are mutable at runtime, so the mount's original source (and
    especially /etc/fstab) can be stale. The control xattrs are authoritative for
    the current branch list and runtime minfreespace reserve. findmnt is used to
    validate the active filesystem and retain any mount options it exposes.
    """
    require_mount(mountpoint, runner)
    fstype = runner(
        ["findmnt", "-n", "-o", "FSTYPE", "--target", str(mountpoint)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if fstype != "fuse.mergerfs":
        raise RuntimeError(f"Required mount is not mergerfs: {mountpoint} ({fstype or 'unknown'})")

    control_file = mountpoint / ".mergerfs"
    result = runner(
        ["getfattr", "--only-values", "-n", runtime_attribute, "--", str(control_file)],
        check=True, capture_output=True, text=True,
    )
    raw_branches = result.stdout.strip()
    if not raw_branches:
        raise RuntimeError("mergerfs returned an empty runtime branch list")

    branches: list[Path] = []
    for entry in raw_branches.split(":"):
        if "=" not in entry:
            raise RuntimeError(f"Invalid mergerfs runtime branch entry: {entry!r}")
        path, mode = entry.rsplit("=", 1)
        if mode != "RW" or not path.startswith("/"):
            raise RuntimeError(f"Unexpected mergerfs runtime branch entry: {entry!r}")
        branches.append(Path(path))
    if len(set(branches)) != len(branches):
        raise RuntimeError("mergerfs returned duplicate runtime branches")

    raw_options = runner(
        ["findmnt", "-n", "-o", "OPTIONS", "--target", str(mountpoint)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    options = _parse_options(raw_options)

    if "minfreespace" in options:
        min_free_bytes = parse_size(str(options["minfreespace"]))
    else:
        min_free_raw = runner(
            ["getfattr", "--only-values", "-n", "user.mergerfs.minfreespace", "--", str(control_file)],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        if not min_free_raw:
            raise RuntimeError("mergerfs returned an empty runtime minfreespace value")
        try:
            min_free_bytes = int(min_free_raw)
        except ValueError as exc:
            raise RuntimeError(f"Invalid mergerfs runtime minfreespace value: {min_free_raw!r}") from exc
        if min_free_bytes < 0:
            raise RuntimeError(f"Invalid mergerfs runtime minfreespace value: {min_free_raw!r}")
        options["minfreespace"] = min_free_raw

    return Pool(mountpoint, branches, options, min_free_bytes)


def require_mount(path: Path, runner=subprocess.run) -> None:
    result = runner(["mountpoint", "-q", str(path)], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Required filesystem is not mounted: {path}")


def backing_source(path: Path, runner=subprocess.run) -> str:
    result = runner(
        ["findmnt", "-n", "-o", "SOURCE", "--target", str(path)],
        check=True, capture_output=True, text=True,
    )
    source = re.sub(r"\[.*\]$", "", result.stdout.strip())
    if not source:
        raise RuntimeError(f"Could not determine backing device for {path}")
    return source


def rotational_for_path(path: Path, runner=subprocess.run) -> bool:
    source = backing_source(path, runner)
    result = runner(
        ["lsblk", "-dn", "-o", "ROTA", source],
        check=True, capture_output=True, text=True,
    )
    value = result.stdout.strip()
    if value not in {"0", "1"}:
        raise RuntimeError(f"Could not determine rotational status for {path}: {value!r}")
    return value == "1"


def stat_branch(path: Path, order: int, runner=subprocess.run) -> Branch:
    info = os.statvfs(path)
    block_size = info.f_frsize or info.f_bsize
    return Branch(
        path, order, rotational_for_path(path, runner),
        info.f_blocks * block_size,
        info.f_bavail * block_size,
        (info.f_blocks - info.f_bfree) * block_size,
    )


def discover_branches(pool: Pool, runner=subprocess.run) -> list[Branch]:
    require_mount(pool.mountpoint, runner)
    branches: list[Branch] = []
    for order, path in enumerate(pool.branches):
        require_mount(path, runner)
        branches.append(stat_branch(path, order, runner))
    return branches
