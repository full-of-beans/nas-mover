from __future__ import annotations

import argparse
import signal
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .config import MoverConfig
from .discovery import discover_branches, discover_runtime_pool, parse_fstab
from .executor import execute_moves
from .locking import process_lock
from .models import PoolConfig
from .planner import plan_moves
from .transfer import MoveCancelled


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Balance mergerfs SSD storage and spill excess to HDD.")
    parser.add_argument("--live", action="store_true", help="Apply the plan; dry-run is the default.")
    parser.add_argument("--config", type=str, default=None, help="Path to an editable TOML configuration file.")
    parser.add_argument("--fstab", type=str, default=None, help="Use a mergerfs entry from this fstab instead of runtime discovery (testing/staging).")
    parser.add_argument("--mount", type=str, default=None, help="Override the configured mergerfs mountpoint.")
    parser.add_argument("--lock", type=str, default=None, help="Override the configured lock path for testing or staging.")
    parser.add_argument("--scope", type=str, default=None, help="Restrict planning to a relative branch directory.")
    parser.add_argument("--watermark", type=float, default=None, help="Override the SSD watermark percentage for testing.")
    parser.add_argument("--tolerance", type=float, default=None, help="Override the SSD watermark tolerance for testing.")
    return parser


def _install_cancel_handlers(cancel_event: threading.Event) -> dict[int, signal.Handlers]:
    previous: dict[int, signal.Handlers] = {}

    def request_cancel(_signum: int, _frame: object) -> None:
        cancel_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, request_cancel)
    return previous


def _restore_signal_handlers(previous: dict[int, signal.Handlers]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = MoverConfig.from_file(Path(args.config)) if args.config else MoverConfig()
    config = replace(
        config,
        fstab_path=Path(args.fstab) if args.fstab else config.fstab_path,
        mount_override=Path(args.mount) if args.mount else config.mount_override,
        lock_path=Path(args.lock) if args.lock else config.lock_path,
        watermark_percent=args.watermark if args.watermark is not None else config.watermark_percent,
        tolerance_percent=args.tolerance if args.tolerance is not None else config.tolerance_percent,
    )
    scope = Path(args.scope) if args.scope else Path(".")
    if scope.is_absolute() or ".." in scope.parts:
        raise ValueError("scope must be a relative directory inside each branch")
    config.validate()
    with process_lock(config.lock_path):
        if args.fstab:
            pool = parse_fstab(config.fstab_path, config.mount_override)
        else:
            pool = discover_runtime_pool(config.mount_override or Path("/mnt/nas/data"))
        branches = discover_branches(pool)
        ssds = [branch for branch in branches if not branch.rotational]
        hdds = [branch for branch in branches if branch.rotational]
        if len(ssds) < 2:
            raise RuntimeError(f"Expected at least two SSD branches; found {len(ssds)}")
        if not hdds:
            raise RuntimeError("No HDD branches were discovered")
        moves = plan_moves(
            ssds, hdds, PoolConfig(pool.min_free_bytes),
            watermark_percent=config.watermark_percent,
            tolerance_percent=config.tolerance_percent,
            policy=config.policy,
            extra_free_percent=config.extra_free_percent,
            scope=scope,
        )
        print(f"{'LIVE' if args.live else 'DRY RUN'}: {len(moves)} move(s) planned")
        for move in moves:
            print(f"{move.reason}: {move.source_path} -> {move.destination_path}")
        if args.live:
            cancel_event = threading.Event()
            previous = _install_cancel_handlers(cancel_event)
            try:
                execute_moves(
                    moves,
                    verify=config.verification,  # type: ignore[arg-type]
                    cancel_event=cancel_event,
                )
            finally:
                _restore_signal_handlers(previous)
    return 0


def main() -> int:
    try:
        return run()
    except MoveCancelled as exc:
        print(f"CANCELLED: {exc}")
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
