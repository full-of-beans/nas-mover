from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from .models import PlannedMove

Verification = Literal["size", "sha256"]
COPY_CHUNK_BYTES = 8 * 1024 * 1024


class MoveCancelled(RuntimeError):
    """Raised when a cooperative mover cancellation is requested."""


def _check_cancel(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise MoveCancelled("Mover cancellation requested")


def _sha256(path: Path, cancel_requested: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            _check_cancel(cancel_requested)
            chunk = handle.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(
    source: Path,
    temp: Path,
    cancel_requested: Callable[[], bool] | None,
) -> None:
    with source.open("rb") as src, temp.open("xb") as dst:
        while True:
            _check_cancel(cancel_requested)
            chunk = src.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    shutil.copystat(source, temp, follow_symlinks=True)


def execute_move(
    move: PlannedMove,
    *,
    verify: Verification = "size",
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    source, destination = move.source_path, move.destination_path
    _check_cancel(cancel_requested)
    if not source.is_file():
        raise RuntimeError(f"Source vanished: {source}")
    if destination.exists():
        raise RuntimeError(f"Destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    temp = destination.with_name(f".nas-mover.{destination.name}.{os.getpid()}.partial")
    try:
        _copy_file(source, temp, cancel_requested)
        _check_cancel(cancel_requested)
        after, copied = source.stat(), temp.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"Source changed during copy: {source}")
        if copied.st_size != after.st_size:
            raise RuntimeError(f"Destination size verification failed: {source}")
        if verify == "sha256" and _sha256(source, cancel_requested) != _sha256(temp, cancel_requested):
            raise RuntimeError(f"SHA-256 verification failed: {source}")
        if verify not in {"size", "sha256"}:
            raise ValueError(f"Unknown verification mode: {verify}")
        _check_cancel(cancel_requested)
        os.replace(temp, destination)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(destination.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        source.unlink()
    except Exception:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise
