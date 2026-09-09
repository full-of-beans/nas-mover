from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

import pytest

import nas_mover.cli as cli
import nas_mover.transfer as transfer
from nas_mover.executor import execute_moves
from nas_mover.models import Branch, PlannedMove
from nas_mover.transfer import MoveCancelled, execute_move


def make_branch(path: Path, *, rotational: bool, order: int) -> Branch:
    path.mkdir(parents=True, exist_ok=True)
    return Branch(path, order, rotational, 10_000, 9_000, 1_000)


def make_move(source: Branch, destination: Branch, name: str, content: bytes = b"payload") -> PlannedMove:
    source_path = source.path / name
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(content)
    stat = source_path.stat()
    return PlannedMove(
        source,
        destination,
        Path(name),
        len(content),
        stat.st_atime,
        stat.st_mtime,
        "test",
    )


def test_execute_move_cooperative_path_completes(tmp_path: Path) -> None:
    source = make_branch(tmp_path / "ssd", rotational=False, order=0)
    destination = make_branch(tmp_path / "hdd", rotational=True, order=1)
    move = make_move(source, destination, "file.bin", b"abcdefgh" * 4)

    execute_move(move, verify="sha256", cancel_requested=lambda: False)

    assert not move.source_path.exists()
    assert move.destination_path.read_bytes() == b"abcdefgh" * 4
    assert not list(destination.path.glob(".nas-mover.*.partial"))


def test_execute_move_cancellation_preserves_source_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_branch(tmp_path / "ssd", rotational=False, order=0)
    destination = make_branch(tmp_path / "hdd", rotational=True, order=1)
    move = make_move(source, destination, "file.bin", b"x" * 64)
    monkeypatch.setattr(transfer, "COPY_CHUNK_BYTES", 4)
    checks = 0

    def cancel_requested() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    with pytest.raises(MoveCancelled, match="cancellation"):
        execute_move(move, cancel_requested=cancel_requested)

    assert move.source_path.read_bytes() == b"x" * 64
    assert not move.destination_path.exists()
    assert not list(destination.path.glob(".nas-mover.*.partial"))


def test_execute_moves_serializes_each_hdd_and_parallelizes_distinct_hdds(tmp_path: Path) -> None:
    source = make_branch(tmp_path / "ssd", rotational=False, order=0)
    hdd1 = make_branch(tmp_path / "hdd1", rotational=True, order=1)
    hdd2 = make_branch(tmp_path / "hdd2", rotational=True, order=2)
    ssd2 = make_branch(tmp_path / "ssd2", rotational=False, order=3)
    moves = [
        make_move(source, ssd2, "ssd-first"),
        make_move(source, hdd1, "hdd1-a"),
        make_move(source, hdd1, "hdd1-b"),
        make_move(source, hdd2, "hdd2-a"),
    ]

    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active_by_destination: dict[str, int] = {}
    max_by_destination: dict[str, int] = {}
    global_active = 0
    global_max = 0
    completed: list[str] = []

    def fake_execute(move: PlannedMove, **_kwargs: object) -> None:
        nonlocal global_active, global_max
        key = str(move.destination_branch.path)
        with lock:
            active_by_destination[key] = active_by_destination.get(key, 0) + 1
            max_by_destination[key] = max(max_by_destination.get(key, 0), active_by_destination[key])
            global_active += 1
            global_max = max(global_max, global_active)
        if move.relative_path.name in {"hdd1-a", "hdd2-a"}:
            barrier.wait(timeout=2)
        time.sleep(0.01)
        with lock:
            completed.append(move.relative_path.name)
            active_by_destination[key] -= 1
            global_active -= 1

    execute_moves(moves, move_executor=fake_execute)

    assert completed[0] == "ssd-first"
    assert max_by_destination[str(hdd1.path)] == 1
    assert max_by_destination[str(hdd2.path)] == 1
    assert global_max == 2
    assert completed.index("hdd1-a") < completed.index("hdd1-b")


def test_execute_moves_honors_preexisting_cancellation_for_sequential_move(tmp_path: Path) -> None:
    source = make_branch(tmp_path / "ssd1", rotational=False, order=0)
    destination = make_branch(tmp_path / "ssd2", rotational=False, order=1)
    move = make_move(source, destination, "file")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(MoveCancelled):
        execute_moves([move], cancel_event=cancel, move_executor=lambda *_args, **_kwargs: None)


def test_execute_moves_honors_preexisting_cancellation_for_hdd_worker(tmp_path: Path) -> None:
    source = make_branch(tmp_path / "ssd", rotational=False, order=0)
    destination = make_branch(tmp_path / "hdd", rotational=True, order=1)
    move = make_move(source, destination, "file")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(MoveCancelled):
        execute_moves([move], cancel_event=cancel, move_executor=lambda *_args, **_kwargs: None)


def test_worker_failure_requests_cancellation_and_joins_other_workers(tmp_path: Path) -> None:
    source = make_branch(tmp_path / "ssd", rotational=False, order=0)
    hdd1 = make_branch(tmp_path / "hdd1", rotational=True, order=1)
    hdd2 = make_branch(tmp_path / "hdd2", rotational=True, order=2)
    moves = [make_move(source, hdd1, "fail"), make_move(source, hdd2, "peer")]
    peer_started = threading.Event()
    peer_finished = threading.Event()

    def fake_execute(move: PlannedMove, *, cancel_requested, **_kwargs: object) -> None:
        if move.relative_path.name == "fail":
            peer_started.wait(timeout=2)
            raise RuntimeError("boom")
        peer_started.set()
        while not cancel_requested():
            time.sleep(0.001)
        peer_finished.set()
        raise MoveCancelled("peer cancelled")

    with pytest.raises(RuntimeError, match="boom"):
        execute_moves(moves, move_executor=fake_execute)

    assert peer_finished.is_set()


def test_signal_handlers_set_event_and_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, object] = {}
    old_handlers = {signal.SIGTERM: object(), signal.SIGINT: object()}
    monkeypatch.setattr(cli.signal, "getsignal", lambda signum: old_handlers[signum])
    monkeypatch.setattr(cli.signal, "signal", lambda signum, handler: installed.__setitem__(signum, handler))
    cancel = threading.Event()

    previous = cli._install_cancel_handlers(cancel)
    assert previous == old_handlers
    handler = installed[signal.SIGTERM]
    assert callable(handler)
    handler(signal.SIGTERM, None)
    assert cancel.is_set()

    installed.clear()
    cli._restore_signal_handlers(previous)
    assert installed == old_handlers


def test_cli_main_reports_cooperative_cancellation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run", lambda: (_ for _ in ()).throw(MoveCancelled("stop")))

    assert cli.main() == 130
    assert "CANCELLED: stop" in capsys.readouterr().out
