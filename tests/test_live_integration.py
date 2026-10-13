import os
from pathlib import Path

import pytest

from nas_mover import Branch, PlannedMove, execute_move
from nas_mover.sandbox import Sandbox


@pytest.mark.integration
def test_live_sandbox_move() -> None:
    sandbox_value = os.environ.get("NAS_MOVER_TEST_SANDBOX")
    if not sandbox_value:
        pytest.skip("Set NAS_MOVER_TEST_SANDBOX to run live sandbox tests")

    try:
        sandbox = Sandbox(Path(sandbox_value))
    except ValueError as exc:
        pytest.fail(str(exc))
    source_root = sandbox.path(Path("source"))
    destination_root = sandbox.path(Path("destination"))
    sandbox.root.mkdir(parents=True, exist_ok=True)
    source_root.mkdir(exist_ok=True)
    destination_root.mkdir(exist_ok=True)
    fixtures = sandbox.create_fixtures(1)
    source = sandbox.path(fixtures[0])

    move = PlannedMove(
        Branch(source_root, 0, False, 100, 90, 10),
        Branch(destination_root, 1, True, 100, 100, 0),
        source.relative_to(source_root),
        source.stat().st_size,
        source.stat().st_atime,
        source.stat().st_mtime,
        "integration",
    )
    execute_move(move, verify="sha256")
    assert (
        destination_root / "test-1.bin"
    ).read_bytes() == b"nas-mover test fixture 1\n"
    assert not source.exists()
    sandbox.remove_fixtures(fixtures)
    (destination_root / "test-1.bin").unlink()
