#!/usr/bin/env python3

"""
NAS SSD -> HDD mover for a mergerfs + SnapRAID storage stack.

Behavior
--------
1. Discover mergerfs branches dynamically from /etc/fstab.
2. Classify branches as SSD/HDD using the backing block device's ROTA value.
3. Rebalance SSD -> SSD first if SSD utilization differs by more than the
   configured tolerance.
4. Once SSDs are balanced, move data SSD -> HDD when the SSD tier is above
   the configured watermark.
5. HDD destination selection uses a configurable mergerfs-style policy.
6. --dry-run builds and reports the exact proposed move plan without making
   any filesystem changes.
7. Real moves copy to a temporary file, verify the source did not change,
   atomically rename the destination, and only then delete the source.

This script DOES NOT run SnapRAID sync. SnapRAID scheduling should occur
separately after the mover completes successfully.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import os
import random
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================================
# USER CONFIGURATION
# ============================================================================

# SSD target/high-water mark.
#
# For normal use:
#     SSD_WATERMARK_PERCENT = 80.0
#
# Lower this temporarily during testing.
SSD_WATERMARK_PERCENT = 1.0

# How close an SSD may be below the watermark and still count as
# effectively "at the watermark" when deciding whether HDDs may be used.
#
# 2% on a ~1 TB SSD is roughly 20 GB.
SSD_WATERMARK_TOLERANCE_PERCENT = 0.2

# Destination-selection polisudo nano /usr/local/sbin/nas-movercy used ONLY for SSD -> HDD moves.
#
# This is independent of mergerfs' own category.create/search/a# How close an SSD may be below the watermark and still count as
# effectively "at the watermark" when deciding whether HDDs may be used.ction policy.
#
# Supported mergerfs-style policies:
#
#   ff, all, rand, pfrd,
#   lfs, lus, lup, mfs, newest,
#   epff, epall, eprand, eppfrd,
#   eplfs, eplus, epmfs,
#   msplfs, msplus, mspmfs, msppfrd
#
MOVER_SEARCH_POLICY = "eplfs"

# Optional minimum age of a file before it is eligible to move.
#
# The age is calculated from:
#
#     max(atime, mtime)
#
# Set to 0 to make all files eligible.
MIN_FILE_AGE_HOURS = 0.0

# Optional extra free-space reserve for HDD destinations.
#
# The mover already honors mergerfs' minfreespace from /etc/fstab.
# Your current mergerfs configuration specifies minfreespace=20G.
#
# Leave this at 0 unless you want an additional percentage reserve.
HDD_EXTRA_FREE_PERCENT = 0.0

# Copy verification:
#
#   "size"   - verify source/destination size and source stability
#   "sha256" - additionally SHA-256 both files before deleting source
#
# "size" is substantially faster for large media libraries.
VERIFY_MODE = "size"

FSTAB_PATH = Path("/etc/fstab")

# If None, require exactly one fuse.mergerfs entry in fstab.
# Set explicitly if you ever add multiple mergerfs pools.
MERGERFS_MOUNT_OVERRIDE: str | None = "/mnt/nas/data"

LOCK_FILE = Path("/run/lock/nas-mover.lock")

# --------------------------------------------------------------------------
# END USER CONFIGURATION
# --------------------------------------------------------------------------


SUPPORTED_POLICIES = {
    "all",
    "epall",
    "epff",
    "eplfs",
    "eplus",
    "epmfs",
    "eppfrd",
    "eprand",
    "ff",
    "lfs",
    "lup",
    "lus",
    "mfs",
    "msplfs",
    "msplus",
    "mspmfs",
    "msppfrd",
    "newest",
    "pfrd",
    "rand",
}


@dataclass
class Branch:
    path: Path
    order: int
    rotational: bool

    total_bytes: int
    free_bytes: int
    used_bytes: int

    # Used for dry-run simulation.
    simulated_free_bytes: int = field(init=False)
    simulated_used_bytes: int = field(init=False)

    # Relative directories which dry-run moves would create.
    planned_dirs: set[Path] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.simulated_free_bytes = self.free_bytes
        self.simulated_used_bytes = self.used_bytes

    @property
    def kind(self) -> str:
        return "HDD" if self.rotational else "SSD"

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.used_bytes / self.total_bytes * 100.0

    @property
    def simulated_used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.simulated_used_bytes / self.total_bytes * 100.0


@dataclass
class CandidateFile:
    source_branch: Branch
    source_path: Path
    relative_path: Path
    size: int
    atime: float
    mtime: float

    @property
    def activity_time(self) -> float:
        return max(self.atime, self.mtime)


@dataclass
class PlannedMove:
    source_branch: Branch
    destination_branch: Branch
    relative_path: Path
    size: int
    atime: float
    mtime: float
    reason: str

    @property
    def activity_time(self) -> float:
        return max(self.atime, self.mtime)

    @property
    def source_path(self) -> Path:
        return self.source_branch.path / self.relative_path

    @property
    def destination_path(self) -> Path:
        return self.destination_branch.path / self.relative_path


@dataclass
class PoolConfig:
    mountpoint: Path
    branches: list[Path]
    options: dict[str, str | bool]
    min_free_bytes: int


def fail(message: str, exit_code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    amount = float(value)

    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024.0

    return f"{value} B"


def format_time(timestamp: float) -> str:
    return (
        dt.datetime.fromtimestamp(timestamp)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )


def parse_size(value: str) -> int:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:i?B?)?\s*",
        value,
        flags=re.IGNORECASE,
    )

    if not match:
        raise ValueError(f"Invalid size: {value}")

    number = float(match.group(1))
    suffix = match.group(2).upper()

    powers = {
        "": 0,
        "K": 1,
        "M": 2,
        "G": 3,
        "T": 4,
        "P": 5,
        "E": 6,
    }

    return int(number * (1024 ** powers[suffix]))


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    handle = LOCK_FILE.open("w")

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail("Another nas-mover instance is already running.")

    handle.write(str(os.getpid()))
    handle.flush()

    return handle


def parse_fstab() -> PoolConfig:
    entries = []

    for raw_line in FSTAB_PATH.read_text().splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        try:
            fields = shlex.split(line, comments=True)
        except ValueError:
            continue

        if len(fields) < 4:
            continue

        source, mountpoint, fstype, options = fields[:4]

        if fstype != "fuse.mergerfs":
            continue

        entries.append((source, mountpoint, options))

    if MERGERFS_MOUNT_OVERRIDE is not None:
        entries = [entry for entry in entries if entry[1] == MERGERFS_MOUNT_OVERRIDE]

    if not entries:
        fail("No matching fuse.mergerfs entry found in /etc/fstab.")

    if len(entries) != 1:
        fail(
            "More than one matching mergerfs entry exists. "
            "Set MERGERFS_MOUNT_OVERRIDE explicitly."
        )

    source, mountpoint, raw_options = entries[0]

    branch_paths = [Path(p) for p in source.split(":")]

    option_map: dict[str, str | bool] = {}

    for option in raw_options.split(","):
        if "=" in option:
            key, value = option.split("=", 1)
            option_map[key] = value
        else:
            option_map[option] = True

    min_free = 0

    if "minfreespace" in option_map:
        min_free = parse_size(str(option_map["minfreespace"]))

    return PoolConfig(
        mountpoint=Path(mountpoint),
        branches=branch_paths,
        options=option_map,
        min_free_bytes=min_free,
    )


def require_mount(path: Path) -> None:
    result = subprocess.run(
        ["mountpoint", "-q", str(path)],
        check=False,
    )

    if result.returncode != 0:
        fail(f"Required filesystem is not mounted: {path}")


def backing_source(path: Path) -> str:
    result = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE", "--target", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )

    source = result.stdout.strip()

    # Btrfs subvolume source may look like:
    #
    # /dev/mapper/foo[/data]
    #
    source = re.sub(r"\[.*\]$", "", source)

    if not source:
        fail(f"Could not determine backing device for {path}")

    return source


def rotational_for_path(path: Path) -> bool:
    source = backing_source(path)

    result = subprocess.run(
        ["lsblk", "-dn", "-o", "ROTA", source],
        check=True,
        capture_output=True,
        text=True,
    )

    value = result.stdout.strip()

    if value not in {"0", "1"}:
        fail(
            f"Could not determine rotational status for {path} "
            f"(source={source!r}, ROTA={value!r})"
        )

    return value == "1"


def stat_branch(path: Path, order: int) -> Branch:
    info = os.statvfs(path)

    block_size = info.f_frsize or info.f_bsize

    total = info.f_blocks * block_size

    # mergerfs free-space policies use f_bavail.
    free = info.f_bavail * block_size

    # Used blocks are based on actual filesystem allocation.
    used = (info.f_blocks - info.f_bfree) * block_size

    return Branch(
        path=path,
        order=order,
        rotational=rotational_for_path(path),
        total_bytes=total,
        free_bytes=free,
        used_bytes=used,
    )


def discover_branches(config: PoolConfig) -> list[Branch]:
    require_mount(config.mountpoint)

    branches = []

    for index, branch_path in enumerate(config.branches):
        require_mount(branch_path)
        branches.append(stat_branch(branch_path, index))

    return branches


def scan_files(branch: Branch) -> list[CandidateFile]:
    candidates = []

    cutoff = dt.datetime.now().timestamp() - MIN_FILE_AGE_HOURS * 3600.0

    for root, dirs, files in os.walk(
        branch.path,
        topdown=True,
        followlinks=False,
    ):
        root_path = Path(root)

        # Never traverse our own temporary files/directories.
        dirs[:] = [d for d in dirs if not d.startswith(".nas-mover.")]

        for filename in files:
            if filename.startswith(".nas-mover."):
                continue

            path = root_path / filename

            try:
                st = path.stat()
            except (FileNotFoundError, PermissionError, OSError):
                continue

            if not path.is_file():
                continue

            activity = max(st.st_atime, st.st_mtime)

            if MIN_FILE_AGE_HOURS > 0 and activity > cutoff:
                continue

            candidates.append(
                CandidateFile(
                    source_branch=branch,
                    source_path=path,
                    relative_path=path.relative_to(branch.path),
                    size=st.st_size,
                    atime=st.st_atime,
                    mtime=st.st_mtime,
                )
            )

    # Coldest files leave SSD first.
    candidates.sort(
        key=lambda item: (
            item.activity_time,
            str(item.relative_path),
        )
    )

    return candidates


def directory_exists_for_plan(branch: Branch, relative_dir: Path) -> bool:
    if relative_dir == Path("."):
        return True

    if relative_dir in branch.planned_dirs:
        return True

    return (branch.path / relative_dir).is_dir()


def closest_existing_parent_depth(
    branch: Branch,
    relative_dir: Path,
) -> int | None:
    """
    Implements the 'most shared path' concept.

    Higher depth means a more-specific shared path.
    Root is depth 0 and therefore always exists.
    """

    current = relative_dir

    while True:
        if directory_exists_for_plan(branch, current):
            return len(current.parts) if current != Path(".") else 0

        if current == Path("."):
            return 0

        current = current.parent


def effective_free(branch: Branch) -> int:
    return branch.simulated_free_bytes


def effective_used(branch: Branch) -> int:
    return branch.simulated_used_bytes


def effective_used_percent(branch: Branch) -> float:
    return branch.simulated_used_percent


def newest_path_mtime(branch: Branch, relative_dir: Path) -> float:
    target = branch.path / relative_dir

    try:
        return target.stat().st_mtime
    except OSError:
        return float("-inf")


def weighted_random_by_free(branches: list[Branch]) -> Branch:
    weights = [max(effective_free(branch), 0) for branch in branches]

    if sum(weights) <= 0:
        return random.choice(branches)

    return random.choices(branches, weights=weights, k=1)[0]


def choose_destination(
    policy: str,
    branches: list[Branch],
    relative_path: Path,
    file_size: int,
    minimum_free_bytes: int,
) -> Branch | None:

    if policy not in SUPPORTED_POLICIES:
        fail(f"Unsupported mover policy: {policy}")

    relative_parent = relative_path.parent

    # Must have enough room for the file AND retain configured reserve.
    candidates = [
        branch
        for branch in branches
        if effective_free(branch) - file_size >= minimum_free_bytes
    ]

    if not candidates:
        return None

    # Maintain mergerfs branch order.
    candidates.sort(key=lambda branch: branch.order)

    # ------------------------------------------------------------------
    # Existing-path policies
    # ------------------------------------------------------------------

    ep_policies = {
        "epall",
        "epff",
        "eplfs",
        "eplus",
        "epmfs",
        "eppfrd",
        "eprand",
    }

    if policy in ep_policies:
        candidates = [
            branch
            for branch in candidates
            if directory_exists_for_plan(branch, relative_parent)
        ]

        if not candidates:
            return None

    # ------------------------------------------------------------------
    # Most-shared-path policies
    # ------------------------------------------------------------------

    msp_policies = {
        "msplfs",
        "msplus",
        "mspmfs",
        "msppfrd",
    }

    if policy in msp_policies:
        depth_map = {
            branch.path: closest_existing_parent_depth(
                branch,
                relative_parent,
            )
            for branch in candidates
        }

        max_depth = max(depth_map.values())

        candidates = [
            branch for branch in candidates if depth_map[branch.path] == max_depth
        ]

    # ------------------------------------------------------------------
    # Policy selection
    # ------------------------------------------------------------------

    if policy in {"ff", "all", "epff", "epall"}:
        return candidates[0]

    if policy in {"rand", "eprand"}:
        return random.choice(candidates)

    if policy in {"pfrd", "eppfrd", "msppfrd"}:
        return weighted_random_by_free(candidates)

    if policy in {"lfs", "eplfs", "msplfs"}:
        return min(
            candidates,
            key=lambda branch: (
                effective_free(branch),
                branch.order,
            ),
        )

    if policy in {"mfs", "epmfs", "mspmfs"}:
        return max(
            candidates,
            key=lambda branch: (
                effective_free(branch),
                -branch.order,
            ),
        )

    if policy in {"lus", "eplus", "msplus"}:
        return min(
            candidates,
            key=lambda branch: (
                effective_used(branch),
                branch.order,
            ),
        )

    if policy == "lup":
        return min(
            candidates,
            key=lambda branch: (
                effective_used_percent(branch),
                branch.order,
            ),
        )

    if policy == "newest":
        return max(
            candidates,
            key=lambda branch: (
                newest_path_mtime(branch, relative_parent),
                -branch.order,
            ),
        )

    fail(f"Policy handler missing for {policy}")
    return None


def apply_simulated_move(
    move: PlannedMove,
) -> None:
    move.source_branch.simulated_used_bytes = max(
        0,
        move.source_branch.simulated_used_bytes - move.size,
    )

    move.source_branch.simulated_free_bytes = min(
        move.source_branch.total_bytes,
        move.source_branch.simulated_free_bytes + move.size,
    )

    move.destination_branch.simulated_used_bytes += move.size
    move.destination_branch.simulated_free_bytes = max(
        0,
        move.destination_branch.simulated_free_bytes - move.size,
    )

    relative_parent = move.relative_path.parent

    current = relative_parent

    while current != Path("."):
        move.destination_branch.planned_dirs.add(current)
        current = current.parent


def planned_destination_exists(
    moves: list[PlannedMove],
    destination: Branch,
    relative_path: Path,
) -> bool:
    return any(
        move.destination_branch.path == destination.path
        and move.relative_path == relative_path
        for move in moves
    )


def real_destination_exists(
    branch: Branch,
    relative_path: Path,
) -> bool:
    return (branch.path / relative_path).exists()


def select_rebalance_target(
    ssds: list[Branch],
    source: Branch,
) -> Branch | None:
    others = [ssd for ssd in ssds if ssd.path != source.path]

    if not others:
        return None

    return min(
        others,
        key=lambda branch: (
            branch.simulated_used_percent,
            branch.order,
        ),
    )


def select_next_file(
    candidates_by_branch: dict[Path, list[CandidateFile]],
    source: Branch,
    already_planned: set[tuple[Path, Path]],
) -> CandidateFile | None:

    for candidate in candidates_by_branch[source.path]:
        key = (source.path, candidate.relative_path)

        if key not in already_planned:
            return candidate

    return None


def plan_moves(
    ssds: list[Branch],
    hdds: list[Branch],
    config: PoolConfig,
) -> list[PlannedMove]:

    moves: list[PlannedMove] = []
    already_planned: set[tuple[Path, Path]] = set()

    candidates_by_branch = {ssd.path: scan_files(ssd) for ssd in ssds}

    # ==================================================================
    # PHASE 1: SSD -> SSD
    #
    # If an SSD is above the configured watermark while another SSD is
    # still materially below it, use the available SSD capacity first.
    #
    # Example with:
    #
    #   watermark = 80
    #   tolerance = 2
    #
    #   SSD1 = 83%
    #   SSD2 = 42%
    #
    # Data moves SSD1 -> SSD2 before HDD storage is considered.
    #
    # HDD movement is permitted only when every SSD is at least:
    #
    #   watermark - tolerance
    #
    # ==================================================================

    lower_watermark = SSD_WATERMARK_PERCENT - SSD_WATERMARK_TOLERANCE_PERCENT

    while True:
        ordered = sorted(
            ssds,
            key=lambda branch: branch.simulated_used_percent,
        )

        least_full = ordered[0]
        most_full = ordered[-1]

        # Nothing needs moving if no SSD exceeds the watermark.
        if most_full.simulated_used_percent <= SSD_WATERMARK_PERCENT:
            break

        # If the least-full SSD is already effectively at the
        # watermark, preserve the excess for Phase 2 (SSD -> HDD).
        if least_full.simulated_used_percent >= lower_watermark:
            break

        candidate = select_next_file(
            candidates_by_branch,
            most_full,
            already_planned,
        )

        if candidate is None:
            break

        destination = least_full

        if destination.simulated_free_bytes < candidate.size:
            already_planned.add((most_full.path, candidate.relative_path))
            continue

        if real_destination_exists(
            destination,
            candidate.relative_path,
        ):
            already_planned.add((most_full.path, candidate.relative_path))
            continue

        if planned_destination_exists(
            moves,
            destination,
            candidate.relative_path,
        ):
            already_planned.add((most_full.path, candidate.relative_path))
            continue

        move = PlannedMove(
            source_branch=most_full,
            destination_branch=destination,
            relative_path=candidate.relative_path,
            size=candidate.size,
            atime=candidate.atime,
            mtime=candidate.mtime,
            reason="SSD -> SSD",
        )

        moves.append(move)

        already_planned.add((most_full.path, candidate.relative_path))

        apply_simulated_move(move)

    # ==================================================================
    # PHASE 2: SSD -> HDD
    #
    # HDDs may be used only when ALL SSDs are effectively at the
    # configured watermark:
    #
    #     SSD >= watermark - tolerance
    #
    # Once that condition is met, any SSD above the actual watermark
    # may shed excess files to HDD storage.
    #
    # HDD destination selection is performed PER FILE using the
    # configured mergerfs-style MOVER_SEARCH_POLICY.
    # ==================================================================

    while True:
        # Do not spill to HDD while any SSD still has materially
        # under-utilized capacity.
        if any(ssd.simulated_used_percent < lower_watermark for ssd in ssds):
            break

        ordered = sorted(
            ssds,
            key=lambda branch: branch.simulated_used_percent,
            reverse=True,
        )

        source = ordered[0]

        # Nothing exceeds the actual watermark anymore.
        if source.simulated_used_percent <= SSD_WATERMARK_PERCENT:
            break

        candidate = select_next_file(
            candidates_by_branch,
            source,
            already_planned,
        )

        if candidate is None:
            # Try another SSD that is above the watermark.
            found = False

            for alternate in ordered[1:]:
                if alternate.simulated_used_percent <= SSD_WATERMARK_PERCENT:
                    continue

                possible = select_next_file(
                    candidates_by_branch,
                    alternate,
                    already_planned,
                )

                if possible is not None:
                    source = alternate
                    candidate = possible
                    found = True
                    break

            if not found:
                break

        # Determine which HDD branches have enough space for this file
        # while preserving mergerfs minfreespace and any additional
        # configured mover reserve.
        eligible_hdds = []

        for hdd in hdds:
            percentage_reserve = int(hdd.total_bytes * (HDD_EXTRA_FREE_PERCENT / 100.0))

            reserve = max(
                config.min_free_bytes,
                percentage_reserve,
            )

            if hdd.simulated_free_bytes - candidate.size >= reserve:
                eligible_hdds.append(hdd)

        if not eligible_hdds:
            break

        # choose_destination() applies the reserve check as well, so use
        # the largest applicable reserve across the current candidates.
        reserve = max(
            max(
                config.min_free_bytes,
                int(hdd.total_bytes * (HDD_EXTRA_FREE_PERCENT / 100.0)),
            )
            for hdd in eligible_hdds
        )

        destination = choose_destination(
            MOVER_SEARCH_POLICY,
            eligible_hdds,
            candidate.relative_path,
            candidate.size,
            reserve,
        )

        if destination is None:
            # For an existing-path policy such as eplfs, this can happen
            # when none of the HDD branches currently contains the
            # candidate file's parent path.
            already_planned.add((source.path, candidate.relative_path))
            continue

        if real_destination_exists(
            destination,
            candidate.relative_path,
        ):
            already_planned.add((source.path, candidate.relative_path))
            continue

        if planned_destination_exists(
            moves,
            destination,
            candidate.relative_path,
        ):
            already_planned.add((source.path, candidate.relative_path))
            continue

        move = PlannedMove(
            source_branch=source,
            destination_branch=destination,
            relative_path=candidate.relative_path,
            size=candidate.size,
            atime=candidate.atime,
            mtime=candidate.mtime,
            reason=f"SSD -> HDD ({MOVER_SEARCH_POLICY})",
        )

        moves.append(move)

        already_planned.add((source.path, candidate.relative_path))

        apply_simulated_move(move)

    return moves


def sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def execute_move(move: PlannedMove) -> None:
    source = move.source_path
    destination = move.destination_path
    destination_parent = destination.parent

    if not source.exists():
        raise RuntimeError(f"Source vanished: {source}")

    if destination.exists():
        raise RuntimeError(f"Destination already exists: {destination}")

    destination_parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    before = source.stat()

    temp_name = f".nas-mover.{destination.name}.{os.getpid()}.partial"

    temp_path = destination_parent / temp_name

    try:
        subprocess.run(
            [
                "cp",
                "-a",
                "--reflink=auto",
                "--sparse=always",
                "--",
                str(source),
                str(temp_path),
            ],
            check=True,
        )

        after = source.stat()
        copied = temp_path.stat()

        # Ensure source was not modified while being copied.
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise RuntimeError(f"Source changed during copy: {source}")

        if copied.st_size != after.st_size:
            raise RuntimeError(f"Destination size verification failed: {source}")

        if VERIFY_MODE == "sha256":
            source_hash = sha256(source)
            destination_hash = sha256(temp_path)

            if source_hash != destination_hash:
                raise RuntimeError(f"SHA-256 verification failed: {source}")

        elif VERIFY_MODE != "size":
            raise RuntimeError(f"Unknown VERIFY_MODE: {VERIFY_MODE}")

        # Atomic within destination filesystem.
        os.replace(temp_path, destination)

        # Flush destination directory metadata before deleting source.
        dir_fd = os.open(destination_parent, os.O_DIRECTORY)

        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        source.unlink()

    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass

        raise


def print_branch_status(
    ssds: list[Branch],
    hdds: list[Branch],
) -> None:

    print()
    print("Discovered mergerfs branches")
    print("============================")

    for branch in ssds + hdds:
        print(
            f"{branch.kind:3}  "
            f"{str(branch.path):32}  "
            f"used={branch.used_percent:6.2f}%  "
            f"free={human_bytes(branch.free_bytes)}"
        )


def print_plan_summary(moves: list[PlannedMove]) -> None:
    print()
    print("Move plan")
    print("=========")

    if not moves:
        print("No files would change drive locations.")
        return

    groups = defaultdict(list)

    for move in moves:
        key = (
            move.source_branch.path,
            move.destination_branch.path,
            move.reason,
        )

        groups[key].append(move)

    header = (
        f"{'FROM':32} "
        f"{'TO':32} "
        f"{'FILES':>8} "
        f"{'SIZE':>12} "
        f"{'OLDEST max(a,m)':>25} "
        f"{'NEWEST max(a,m)':>25}"
    )

    print(header)
    print("-" * len(header))

    for (
        source,
        destination,
        reason,
    ), group in sorted(
        groups.items(),
        key=lambda item: (
            str(item[0][0]),
            str(item[0][1]),
        ),
    ):
        total_size = sum(move.size for move in group)
        oldest = min(move.activity_time for move in group)
        newest = max(move.activity_time for move in group)

        print(
            f"{source.parent.name:32} "
            f"{destination.parent.name:32} "
            f"{len(group):8d} "
            f"{human_bytes(total_size):>12} "
            f"{format_time(oldest):>25} "
            f"{format_time(newest):>25}"
        )

        print(f"    reason: {reason}")

    total_size = sum(move.size for move in moves)
    oldest = min(move.activity_time for move in moves)
    newest = max(move.activity_time for move in moves)

    print()
    print("TOTAL")
    print("-----")
    print(f"Files:  {len(moves)}")
    print(f"Size:   {human_bytes(total_size)}")
    print(f"Oldest: {format_time(oldest)}")
    print(f"Newest: {format_time(newest)}")


def print_simulated_final_state(
    ssds: list[Branch],
    hdds: list[Branch],
) -> None:
    print()
    print("Projected branch utilization")
    print("============================")

    for branch in ssds + hdds:
        print(
            f"{branch.kind:3}  "
            f"{str(branch.path):32}  "
            f"{branch.used_percent:6.2f}% -> "
            f"{branch.simulated_used_percent:6.2f}%"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Balance mergerfs SSD tier and move excess data to HDD."
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan and summarize moves without changing files.",
    )

    args = parser.parse_args()

    if not 0 <= SSD_WATERMARK_PERCENT <= 100:
        fail("SSD_WATERMARK_PERCENT must be between 0 and 100.")

    if not 0 <= SSD_WATERMARK_TOLERANCE_PERCENT <= 100:
        fail("SSD_WATERMARK_TOLERANCE_PERCENT must be between 0 and 100.")

    if MOVER_SEARCH_POLICY not in SUPPORTED_POLICIES:
        fail(f"MOVER_SEARCH_POLICY={MOVER_SEARCH_POLICY!r} is not supported.")

    if VERIFY_MODE not in {"size", "sha256"}:
        fail("VERIFY_MODE must be 'size' or 'sha256'.")

    lock_handle = acquire_lock()

    # Keep lock_handle alive for entire process.
    _ = lock_handle

    config = parse_fstab()

    branches = discover_branches(config)

    ssds = [branch for branch in branches if not branch.rotational]

    hdds = [branch for branch in branches if branch.rotational]

    if len(ssds) < 2:
        fail(f"Expected at least two SSD mergerfs branches; found {len(ssds)}.")

    if not hdds:
        fail("No HDD mergerfs branches were discovered.")

    print("NAS Mover")
    print("=========")
    print(f"Mode:                    {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"MergerFS pool:           {config.mountpoint}")
    print(f"SSD watermark:           {SSD_WATERMARK_PERCENT:.2f}%")
    print(f"SSD watermark tolerance:   {SSD_WATERMARK_TOLERANCE_PERCENT:.2f}%")
    print(f"HDD destination policy:  {MOVER_SEARCH_POLICY}")
    print(f"MergerFS minfreespace:   {human_bytes(config.min_free_bytes)}")
    print(f"Minimum file age:        {MIN_FILE_AGE_HOURS:.2f} hours")
    print(f"Verification:            {VERIFY_MODE}")

    print_branch_status(ssds, hdds)

    moves = plan_moves(
        ssds,
        hdds,
        config,
    )

    print_plan_summary(moves)
    print_simulated_final_state(ssds, hdds)

    if args.dry_run:
        print()
        print("DRY RUN: no filesystem changes were made.")
        return 0

    if not moves:
        print()
        print("Nothing to move.")
        return 0

    print()
    print("Executing move plan")
    print("===================")

    completed = 0
    failed = 0

    for index, move in enumerate(moves, start=1):
        print(
            f"[{index}/{len(moves)}] "
            f"{move.source_branch.path.name} -> "
            f"{move.destination_branch.path.name}: "
            f"{move.relative_path} "
            f"({human_bytes(move.size)})"
        )

        try:
            execute_move(move)
            completed += 1

        except Exception as exc:
            failed += 1
            print(
                f"FAILED: {move.relative_path}: {exc}",
                file=sys.stderr,
            )

    print()
    print("Mover complete")
    print("==============")
    print(f"Completed: {completed}")
    print(f"Failed:    {failed}")

    if failed:
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
