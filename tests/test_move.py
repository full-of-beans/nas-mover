import os
from pathlib import Path

import pytest

from nas_mover import Branch, PlannedMove, execute_move


def make_move(
    tmp_path: Path, content: bytes = b"nas data"
) -> tuple[PlannedMove, Path, Path]:
    source_root, destination_root = tmp_path / "source", tmp_path / "destination"
    source_root.mkdir()
    destination_root.mkdir()
    source = source_root / "nested" / "file.bin"
    source.parent.mkdir()
    source.write_bytes(content)
    source_branch = Branch(source_root, 0, False, 1000, 900, 100)
    destination_branch = Branch(destination_root, 1, True, 1000, 1000, 0)
    move = PlannedMove(
        source_branch,
        destination_branch,
        Path("nested/file.bin"),
        len(content),
        source.stat().st_atime,
        source.stat().st_mtime,
        "test",
    )
    return move, source, destination_root / "nested/file.bin"


def test_execute_move_copies_verifies_and_deletes_source(tmp_path: Path) -> None:
    move, source, destination = make_move(tmp_path)
    execute_move(move, verify="sha256")
    assert not source.exists()
    assert destination.read_bytes() == b"nas data"
    assert not list(destination.parent.glob(".nas-mover.*.partial"))


def test_execute_move_refuses_existing_destination_and_preserves_source(
    tmp_path: Path,
) -> None:
    move, source, destination = make_move(tmp_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="already exists"):
        execute_move(move)
    assert source.exists()
    assert destination.read_bytes() == b"existing"


def test_execute_move_rejects_vanished_source(tmp_path: Path) -> None:
    move, source, _ = make_move(tmp_path)
    source.unlink()
    with pytest.raises(RuntimeError, match="vanished"):
        execute_move(move)


def test_execute_move_removes_bad_temporary_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    move, source, _ = make_move(tmp_path)

    def bad_copy(source_path: Path, destination_path: Path) -> None:
        destination_path.write_bytes(b"wrong!!!")

    monkeypatch.setattr("nas_mover.transfer.shutil.copy2", bad_copy)
    with pytest.raises(RuntimeError, match="SHA-256"):
        execute_move(move, verify="sha256")
    assert source.exists()
    assert not list((tmp_path / "destination" / "nested").glob(".nas-mover.*.partial"))


def test_execute_move_refuses_source_changed_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    move, source, _ = make_move(tmp_path)
    original_copy = __import__("shutil").copy2

    def changing_copy(source_path: Path, destination_path: Path) -> None:
        original_copy(source_path, destination_path)
        source_stat = source_path.stat()
        source_path.touch()
        os.utime(
            source_path,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns + 1_000_000_000),
        )

    monkeypatch.setattr("nas_mover.transfer.shutil.copy2", changing_copy)
    with pytest.raises(RuntimeError, match="changed"):
        execute_move(move)
    assert source.exists()


def test_execute_move_refuses_size_mismatch_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    move, source, destination = make_move(tmp_path)
    monkeypatch.setattr(
        "nas_mover.transfer.shutil.copy2",
        lambda source_path, destination_path: destination_path.write_bytes(b"x"),
    )
    with pytest.raises(RuntimeError, match="size verification"):
        execute_move(move)
    assert source.exists()
    assert not destination.exists()


def test_execute_move_cleans_temp_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    move, source, destination = make_move(tmp_path)
    monkeypatch.setattr(
        "nas_mover.transfer.os.replace",
        lambda *args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        execute_move(move)
    assert source.exists()
    assert not destination.exists()
    assert not list(destination.parent.glob(".nas-mover.*.partial"))


def test_execute_move_flushes_destination_directory_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    move, source, destination = make_move(tmp_path)
    import nas_mover.transfer as transfer

    monkeypatch.setattr(transfer.os, "O_DIRECTORY", 1, raising=False)
    monkeypatch.setattr(transfer.os, "open", lambda *args: 42)
    monkeypatch.setattr(transfer.os, "fsync", lambda fd: None)
    monkeypatch.setattr(transfer.os, "close", lambda fd: None)
    execute_move(move)
    assert destination.exists()
    assert not source.exists()


def test_execute_move_tolerates_missing_temp_during_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    move, source, _ = make_move(tmp_path)

    def replace_then_fail(temp_path: Path, destination_path: Path) -> None:
        temp_path.unlink()
        raise OSError("replace failed")

    monkeypatch.setattr("nas_mover.transfer.os.replace", replace_then_fail)
    with pytest.raises(OSError, match="replace failed"):
        execute_move(move)
    assert source.exists()


def test_execute_move_supports_platforms_without_directory_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    move, source, destination = make_move(tmp_path)
    import nas_mover.transfer as transfer

    monkeypatch.delattr(transfer.os, "O_DIRECTORY", raising=False)
    execute_move(move)
    assert destination.exists()
    assert not source.exists()


def test_execute_move_rejects_unknown_verification_mode(tmp_path: Path) -> None:
    move, source, _ = make_move(tmp_path)
    with pytest.raises(ValueError, match="Unknown verification"):
        execute_move(move, verify="crc")  # type: ignore[arg-type]
    assert source.exists()
