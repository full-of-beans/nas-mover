from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .config import MoverConfig
from .discovery import discover_branches, parse_fstab
from .locking import process_lock
from .models import PoolConfig
from .planner import plan_moves
from .sandbox import Sandbox
from .transfer import execute_move


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run pytest and a scoped NAS mover integration test."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to an editable TOML configuration file.",
    )
    parser.add_argument("--fstab", type=Path, default=None)
    parser.add_argument("--mount", type=Path, default=None)
    parser.add_argument(
        "--scope",
        type=Path,
        required=True,
        help="Relative test directory on every branch.",
    )
    parser.add_argument("--lock", type=Path, default=None)
    parser.add_argument(
        "--live", action="store_true", help="Apply the scoped integration plan."
    )
    return parser


def _validate_scope(scope: Path) -> None:
    if scope.is_absolute() or ".." in scope.parts or scope == Path("."):
        raise ValueError("scope must be a non-root relative directory")


def _run_pytest() -> None:
    result = subprocess.run([sys.executable, "-m", "pytest"], check=False)
    if result.returncode:
        raise RuntimeError(f"pytest failed with exit code {result.returncode}")


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_scope(args.scope)
    config = MoverConfig.from_file(args.config) if args.config else MoverConfig()
    config = replace(
        config,
        fstab_path=args.fstab if args.fstab else config.fstab_path,
        mount_override=args.mount if args.mount else config.mount_override,
        lock_path=args.lock if args.lock else config.lock_path,
    )
    config.validate()
    _run_pytest()

    with process_lock(config.lock_path):
        pool = parse_fstab(config.fstab_path, config.mount_override)
        branches = discover_branches(pool)
        ssds = [branch for branch in branches if not branch.rotational]
        hdds = [branch for branch in branches if branch.rotational]
        if len(ssds) < 2 or not hdds:
            raise RuntimeError(
                "Integration requires at least two SSD branches and one HDD branch"
            )
        for branch in branches:
            branch_scope = Sandbox(branch.path / args.scope)
            branch_scope.root.mkdir(parents=True, exist_ok=True)
        fixture_scope = Sandbox(ssds[0].path / args.scope)
        relative_fixtures = [Path(f"test-{index}.bin") for index in range(1, 7)]
        if any(fixture_scope.path(relative).exists() for relative in relative_fixtures):
            raise RuntimeError("Refusing to overwrite existing integration fixtures")
        for index, relative in enumerate(relative_fixtures, start=1):
            fixture_scope.create_fixture(
                relative, f"nas-mover test fixture {index}\n".encode()
            )
        try:
            moves = plan_moves(
                ssds,
                hdds,
                PoolConfig(pool.min_free_bytes),
                watermark_percent=0,
                tolerance_percent=0,
                policy=config.policy,
                extra_free_percent=config.extra_free_percent,
                scope=args.scope,
            )
            if len(moves) != len(relative_fixtures):
                raise RuntimeError(
                    f"Expected {len(relative_fixtures)} scoped moves, planned {len(moves)}"
                )
            print(
                f"{'LIVE' if args.live else 'DRY RUN'}: {len(moves)} scoped move(s) planned"
            )
            for move in moves:
                print(f"{move.reason}: {move.source_path} -> {move.destination_path}")
            if not args.live:
                return 0
            for move in moves:
                execute_move(move, verify="sha256")
            for move in moves:
                if move.source_path.exists() or not move.destination_path.is_file():
                    raise RuntimeError(
                        f"Integration verification failed: {move.relative_path}"
                    )
                expected = f"nas-mover test fixture {move.relative_path.stem.removeprefix('test-')}\n".encode()
                if _hash(move.destination_path) != hashlib.sha256(expected).hexdigest():
                    raise RuntimeError(
                        f"Integration hash verification failed: {move.relative_path}"
                    )
            print("LIVE INTEGRATION: copy, verify, delete, and hash checks passed")
            return 0
        finally:
            for branch in branches:
                branch_scope = Sandbox(branch.path / args.scope)
                for relative in relative_fixtures:
                    branch_scope.remove_fixture(relative)


def main() -> int:
    try:
        return run()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
