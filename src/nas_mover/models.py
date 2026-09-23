from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Branch:
    path: Path
    order: int
    rotational: bool
    total_bytes: int
    free_bytes: int
    used_bytes: int
    min_free_bytes: int = 0
    simulated_free_bytes: int = field(init=False)
    simulated_used_bytes: int = field(init=False)
    planned_dirs: set[Path] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.simulated_free_bytes = self.free_bytes
        self.simulated_used_bytes = self.used_bytes

    @property
    def kind(self) -> str:
        return "HDD" if self.rotational else "SSD"

    @property
    def used_percent(self) -> float:
        return self.used_bytes / self.total_bytes * 100 if self.total_bytes else 0

    @property
    def simulated_used_percent(self) -> float:
        return (
            self.simulated_used_bytes / self.total_bytes * 100
            if self.total_bytes else 0
        )


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class PlannedMove:
    source_branch: Branch
    destination_branch: Branch
    relative_path: Path
    size: int
    atime: float
    mtime: float
    reason: str

    @property
    def source_path(self) -> Path:
        return self.source_branch.path / self.relative_path

    @property
    def destination_path(self) -> Path:
        return self.destination_branch.path / self.relative_path


@dataclass(frozen=True)
class PoolConfig:
    min_free_bytes: int = 0
