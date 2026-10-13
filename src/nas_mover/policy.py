from __future__ import annotations

import random
from pathlib import Path
from typing import Literal

from .models import Branch

Policy = Literal[
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
]
SUPPORTED_POLICIES = set(Policy.__args__)


def _directory_exists(branch: Branch, relative_dir: Path) -> bool:
    return (
        relative_dir == Path(".")
        or relative_dir in branch.planned_dirs
        or (branch.path / relative_dir).is_dir()
    )


def _shared_depth(branch: Branch, relative_dir: Path) -> int:
    current = relative_dir
    while current != Path(".") and not _directory_exists(branch, current):
        current = current.parent
    return len(current.parts) if current != Path(".") else 0


def _weighted_random(branches: list[Branch]) -> Branch:
    weights = [max(branch.simulated_free_bytes, 0) for branch in branches]
    return (
        random.choice(branches)
        if not sum(weights)
        else random.choices(branches, weights=weights, k=1)[0]
    )


def choose_destination(
    policy: Policy,
    branches: list[Branch],
    relative_path: Path,
    file_size: int,
    minimum_free_bytes: int = 0,
) -> Branch | None:
    if policy not in SUPPORTED_POLICIES:
        raise ValueError(f"Unsupported mover policy: {policy}")
    candidates = [
        branch
        for branch in branches
        if branch.simulated_free_bytes - file_size >= minimum_free_bytes
    ]
    candidates.sort(key=lambda branch: branch.order)
    if policy.startswith("ep"):
        candidates = [
            b for b in candidates if _directory_exists(b, relative_path.parent)
        ]
    if policy.startswith("msp"):
        if not candidates:
            return None
        depth = max(_shared_depth(b, relative_path.parent) for b in candidates)
        candidates = [
            b for b in candidates if _shared_depth(b, relative_path.parent) == depth
        ]
    if not candidates:
        return None
    if policy in {"ff", "all", "epff", "epall"}:
        return candidates[0]
    if policy in {"rand", "eprand"}:
        return random.choice(candidates)
    if policy in {"pfrd", "eppfrd", "msppfrd"}:
        return _weighted_random(candidates)
    if policy in {"lfs", "eplfs", "msplfs"}:
        return min(candidates, key=lambda b: (b.simulated_free_bytes, b.order))
    if policy in {"mfs", "epmfs", "mspmfs"}:
        return max(candidates, key=lambda b: (b.simulated_free_bytes, -b.order))
    if policy in {"lus", "eplus", "msplus"}:
        return min(candidates, key=lambda b: (b.simulated_used_bytes, b.order))
    if policy == "lup":
        return min(candidates, key=lambda b: (b.simulated_used_percent, b.order))
    if policy == "newest":
        return max(
            candidates,
            key=lambda b: (
                (b.path / relative_path.parent).stat().st_mtime
                if (b.path / relative_path.parent).exists()
                else float("-inf"),
                -b.order,
            ),
        )
    raise ValueError(f"Policy handler missing for {policy}")
