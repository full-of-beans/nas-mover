from __future__ import annotations

import os
from pathlib import Path

from .models import Branch, CandidateFile, PlannedMove, PoolConfig
from .policy import Policy, choose_destination
from .accounting import is_excluded


def scan_files(branch: Branch, scope: Path = Path("."), excluded_paths: tuple[Path, ...] = ()) -> list[CandidateFile]:
    try:
        (branch.path / scope).resolve().relative_to(branch.path.resolve())
    except ValueError as exc:
        raise ValueError(f"Scope must remain inside branch: {scope}") from exc
    candidates: list[CandidateFile] = []
    if is_excluded(scope, excluded_paths):
        return candidates
    for root, dirs, files in os.walk(branch.path / scope, topdown=True, followlinks=False):
        relative = Path(root).relative_to(branch.path)
        dirs[:] = [name for name in dirs if not name.startswith(".nas-mover.")
                   and not is_excluded(relative / name, excluded_paths)]
        for name in files:
            if name.startswith(".nas-mover.") or is_excluded(relative / name, excluded_paths):
                continue
            path = Path(root) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file() and not path.is_symlink():
                candidates.append(CandidateFile(
                    branch, path, path.relative_to(branch.path), stat.st_size,
                    stat.st_atime, stat.st_mtime,
                ))
    return sorted(candidates, key=lambda item: (item.activity_time, str(item.relative_path)))


def _apply(move: PlannedMove) -> None:
    source, destination = move.source_branch, move.destination_branch
    source.simulated_used_bytes = max(0, source.simulated_used_bytes - move.size)
    source.simulated_free_bytes = min(source.total_bytes, source.simulated_free_bytes + move.size)
    destination.simulated_used_bytes += move.size
    destination.simulated_free_bytes = max(0, destination.simulated_free_bytes - move.size)
    parent = move.relative_path.parent
    while parent != Path("."):
        destination.planned_dirs.add(parent)
        parent = parent.parent


def plan_moves(
    ssds: list[Branch],
    hdds: list[Branch],
    config: PoolConfig,
    *,
    watermark_percent: float,
    tolerance_percent: float,
    policy: Policy,
    extra_free_percent: float = 0,
    scope: Path = Path("."),
    excluded_paths: tuple[Path, ...] = (),
) -> list[PlannedMove]:
    if not ssds:
        return []
    candidates = {branch.path: scan_files(branch, scope, excluded_paths) for branch in ssds}
    planned: list[PlannedMove] = []
    used: set[tuple[Path, Path]] = set()
    lower = watermark_percent - tolerance_percent

    def next_file(branch: Branch) -> CandidateFile | None:
        return next((item for item in candidates[branch.path] if (branch.path, item.relative_path) not in used), None)

    while True:
        least = min(ssds, key=lambda b: b.simulated_used_percent)
        most = max(ssds, key=lambda b: b.simulated_used_percent)
        if most.simulated_used_percent <= watermark_percent or least.simulated_used_percent >= lower:
            break
        candidate = next_file(most)
        if candidate is None:
            break
        used.add((most.path, candidate.relative_path))
        if (least.simulated_free_bytes - candidate.size < max(config.min_free_bytes, least.min_free_bytes)
                or (least.path / candidate.relative_path).exists()):
            continue
        move = PlannedMove(most, least, candidate.relative_path, candidate.size, candidate.atime, candidate.mtime, "SSD -> SSD")
        planned.append(move)
        _apply(move)

    while hdds and all(branch.simulated_used_percent >= lower for branch in ssds):
        source = max(ssds, key=lambda b: b.simulated_used_percent)
        if source.simulated_used_percent <= watermark_percent:
            break
        candidate = next_file(source)
        if candidate is None:
            for alternate in sorted(ssds, key=lambda b: b.simulated_used_percent, reverse=True):
                if alternate is source or alternate.simulated_used_percent <= watermark_percent:
                    continue
                possible = next_file(alternate)
                if possible is not None:
                    source, candidate = alternate, possible
                    break
            else:
                break
        reserves = [max(config.min_free_bytes, h.min_free_bytes, int(h.total_bytes * extra_free_percent / 100)) for h in hdds]
        eligible = [h for h, reserve in zip(hdds, reserves) if h.simulated_free_bytes - candidate.size >= reserve]
        destination = choose_destination(policy, eligible, candidate.relative_path, candidate.size, max(reserves, default=0))
        used.add((source.path, candidate.relative_path))
        if destination is None or (destination.path / candidate.relative_path).exists():
            continue
        move = PlannedMove(source, destination, candidate.relative_path, candidate.size, candidate.atime, candidate.mtime, f"SSD -> HDD ({policy})")
        planned.append(move)
        _apply(move)
    return planned
