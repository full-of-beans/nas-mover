from pathlib import Path

from nas_mover.executor import execute_moves
from nas_mover.models import Branch, PlannedMove


def test_sequential_only_plan_completes_without_hdd_workers(tmp_path: Path) -> None:
    source = Branch(tmp_path / "ssd1", 0, False, 1000, 900, 100)
    destination = Branch(tmp_path / "ssd2", 1, False, 1000, 900, 100)
    seen: list[PlannedMove] = []
    move = PlannedMove(source, destination, Path("file"), 1, 0, 0, "balance")

    execute_moves([move], move_executor=lambda item, **_kwargs: seen.append(item))

    assert seen == [move]
