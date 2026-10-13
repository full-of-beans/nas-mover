from __future__ import annotations

import argparse
from pathlib import Path

from .sandbox import Sandbox


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or remove NAS mover test fixtures in a dedicated sandbox."
    )
    parser.add_argument(
        "sandbox",
        type=Path,
        help="Dedicated sandbox directory; production mounts are refused.",
    )
    parser.add_argument(
        "--count", type=int, default=6, help="Number of test files to create."
    )
    parser.add_argument(
        "--cleanup", action="store_true", help="Remove only the named test files."
    )
    args = parser.parse_args()
    sandbox = Sandbox(args.sandbox)
    fixtures = sandbox.fixture_paths(args.count)
    if args.cleanup:
        sandbox.remove_fixtures(fixtures)
        print(f"Removed {len(fixtures)} fixture(s) from {sandbox.root}")
    else:
        sandbox.root.mkdir(parents=True, exist_ok=True)
        sandbox.create_fixtures(args.count)
        print(f"Created {len(fixtures)} fixture(s) under {sandbox.root / 'source'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
