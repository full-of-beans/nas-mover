"""SSD-side excluded-path accounting; a snapshot is published only after a full scan."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import Branch


def is_excluded(path: Path, exclusions: tuple[Path, ...]) -> bool:
    return any(path == excluded or excluded in path.parents for excluded in exclusions)


def _measure(path: Path) -> tuple[int, int, int]:
    """Return allocated bytes, apparent bytes, and regular-file count."""
    stat = path.lstat()
    allocated = stat.st_blocks * 512
    if not path.is_dir() or path.is_symlink():
        return allocated, stat.st_size if path.is_file() and not path.is_symlink() else 0, int(path.is_file() and not path.is_symlink())
    apparent = files = 0
    with os.scandir(path) as entries:
        for entry in entries:
            child_allocated, child_apparent, child_files = _measure(Path(entry.path))
            allocated += child_allocated
            apparent += child_apparent
            files += child_files
    return allocated, apparent, files


def scan_exclusions(ssds: list[Branch], exclusions: tuple[Path, ...]) -> dict:
    """Never inspect rotational branches; overlapping paths count once per SSD."""
    unique = set(exclusions)
    roots = tuple(sorted((path for path in unique if not is_excluded(path, tuple(unique - {path}))), key=str))
    observed = datetime.now(timezone.utc).isoformat()
    records = []
    for branch in ssds:
        for relative in roots:
            target = branch.path / relative
            # Missing paths are explicit; a missing configured path must not read as zero use.
            try:
                target.lstat()
            except FileNotFoundError:
                records.append(dict(branch=str(branch.path), path=relative.as_posix(), present=False,
                                    allocated_bytes=None, apparent_bytes=None, files=None))
                continue
            allocated, apparent, files = _measure(target)
            records.append(dict(branch=str(branch.path), path=relative.as_posix(), present=True,
                                allocated_bytes=allocated, apparent_bytes=apparent, files=files))
    return dict(schema_version=1, observed_at=observed, scope="ssd_only", complete=True,
                excluded_paths=records)


def publish_snapshot(snapshot: dict, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".nas-mover.", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(snapshot, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
