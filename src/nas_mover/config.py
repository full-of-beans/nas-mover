from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from dataclasses import replace

from .policy import Policy, SUPPORTED_POLICIES


@dataclass(frozen=True)
class MoverConfig:
    watermark_percent: float = 80.0
    tolerance_percent: float = 2.0
    policy: Policy = "eplfs"
    min_file_age_hours: float = 0.0
    extra_free_percent: float = 0.0
    verification: str = "size"
    fstab_path: Path = Path("/etc/fstab")
    # None means select the sole fuse.mergerfs entry from fstab automatically.
    mount_override: Path | None = None
    lock_path: Path = Path("/run/lock/nas-mover.lock")
    excluded_paths: tuple[Path, ...] = ()
    accounting_path: Path = Path("/var/lib/nas-mover/excluded-accounting.json")

    @classmethod
    def from_file(cls, path: Path) -> "MoverConfig":
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with path.open("rb") as handle:
            values = tomllib.load(handle)
        unknown = set(values) - {
            "watermark_percent", "tolerance_percent", "policy",
            "min_file_age_hours", "extra_free_percent", "verification",
            "fstab_path", "mount_override", "lock_path", "excluded_paths", "accounting_path",
        }
        if unknown:
            raise ValueError(f"Unknown configuration option(s): {', '.join(sorted(unknown))}")
        path_values = {
            "fstab_path": Path,
            "mount_override": lambda value: Path(value) if value is not None else None,
            "lock_path": Path,
            "accounting_path": Path,
            "excluded_paths": lambda entries: tuple(Path(entry) for entry in entries),
        }
        for key, converter in path_values.items():
            if key in values:
                values[key] = converter(values[key])
        config = replace(cls(), **values)
        config.validate()
        return config

    def validate(self) -> None:
        if not 0 <= self.watermark_percent <= 100:
            raise ValueError("watermark_percent must be between 0 and 100")
        if not 0 <= self.tolerance_percent <= 100:
            raise ValueError("tolerance_percent must be between 0 and 100")
        if self.policy not in SUPPORTED_POLICIES:
            raise ValueError(f"Unsupported mover policy: {self.policy}")
        if self.min_file_age_hours < 0:
            raise ValueError("min_file_age_hours cannot be negative")
        if self.extra_free_percent < 0:
            raise ValueError("extra_free_percent cannot be negative")
        if self.verification not in {"size", "sha256"}:
            raise ValueError("verification must be 'size' or 'sha256'")
        for path in self.excluded_paths:
            if path == Path(".") or path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"Excluded paths must be relative directories/files inside a branch: {path}")


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:i?B?)?\s*", value, re.IGNORECASE)
    if not match:
        raise ValueError(f"Invalid size: {value}")
    powers = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}
    return int(float(match.group(1)) * 1024 ** powers[match.group(2).upper()])
