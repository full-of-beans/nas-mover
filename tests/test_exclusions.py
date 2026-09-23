from pathlib import Path

import pytest

from nas_mover.accounting import is_excluded, publish_snapshot, scan_exclusions
from nas_mover.config import MoverConfig
from nas_mover.models import Branch, PlannedMove
from nas_mover.planner import scan_files
from nas_mover.transfer import execute_move
from nas_mover.cli import run
from nas_mover.discovery import Pool
import json


def branch(path: Path) -> Branch:
    path.mkdir()
    return Branch(path, 0, False, 100000, 100, 99900)


def test_recursive_paths_and_boundaries(tmp_path):
    ssd = branch(tmp_path / "ssd")
    for name in ("data/.pbs/a/chunk", "data/.pbs-extra/keep", "data/other/protected/deep/skip", "data/keep"):
        target = ssd.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("payload")
    exclusions = (Path("data/.pbs"), Path("data/other/protected"))
    assert is_excluded(Path("data/.pbs/a/chunk"), exclusions)
    assert not is_excluded(Path("data/.pbs-extra/keep"), exclusions)
    assert {str(item.relative_path) for item in scan_files(ssd, excluded_paths=exclusions)} == {
        "data/.pbs-extra/keep", "data/keep"
    }
    snapshot = scan_exclusions([ssd], exclusions + (Path("data/.pbs/a"), Path("data/.pbs")))
    assert snapshot["schema_version"] == 1
    assert len(snapshot["excluded_paths"]) == 2
    assert sum(row["files"] for row in snapshot["excluded_paths"]) == 2
    missing = scan_exclusions([ssd], (Path("missing"),))
    assert missing["excluded_paths"][0]["present"] is False
    assert missing["excluded_paths"][0]["allocated_bytes"] is None
    assert scan_files(ssd, Path("data/.pbs/a"), exclusions) == []
    (ssd.path / "data" / "alias").symlink_to(ssd.path / "data/.pbs/a/chunk")
    assert Path("data/alias") not in {item.relative_path for item in scan_files(ssd, excluded_paths=exclusions)}


def test_failed_accounting_preserves_last_good(tmp_path, monkeypatch):
    ssd = branch(tmp_path / "ssd")
    target = ssd.path / "protected"
    target.mkdir()
    (target / "file").write_text("ok")
    destination = tmp_path / "snapshot.json"
    good = scan_exclusions([ssd], (Path("protected"),))
    publish_snapshot(good, destination)
    before = destination.read_bytes()
    from nas_mover import accounting
    monkeypatch.setattr(accounting, "_measure", lambda _: (_ for _ in ()).throw(OSError("read failure")))
    with pytest.raises(OSError):
        publish_snapshot(scan_exclusions([ssd], (Path("protected"),)), destination)
    assert destination.read_bytes() == before


def test_stale_plan_rechecked_before_transfer(tmp_path):
    src, dst = branch(tmp_path / "src"), branch(tmp_path / "dst")
    path = src.path / "a" / "file"
    path.parent.mkdir()
    path.write_text("original")
    move = PlannedMove(src, dst, Path("a/file"), 8, 0, 0, "test")
    with pytest.raises(RuntimeError, match="became excluded"):
        execute_move(move, exclusions_provider=lambda: (Path("a"),))
    assert path.read_text() == "original"
    assert not (dst.path / "a").exists()


def test_exclusion_appears_during_copy(tmp_path, monkeypatch):
    src, dst = branch(tmp_path / "src"), branch(tmp_path / "dst")
    path = src.path / "file"
    path.write_text("original")
    move = PlannedMove(src, dst, Path("file"), 8, 0, 0, "test")
    from nas_mover import transfer
    original = transfer._copy_file
    changed = []
    def copy(*args):
        original(*args)
        changed.append(True)
    monkeypatch.setattr(transfer, "_copy_file", copy)
    with pytest.raises(RuntimeError, match="during transfer"):
        execute_move(move, exclusions_provider=lambda: (Path("file"),) if changed else ())
    assert path.exists()
    assert not (dst.path / "file").exists()


def test_stale_plan_symlink_rename_rejected(tmp_path):
    src, dst = branch(tmp_path / "src"), branch(tmp_path / "dst")
    (src.path / "folder").mkdir()
    (src.path / "folder" / "file").write_text("ordinary")
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "file").write_text("secret")
    move = PlannedMove(src, dst, Path("folder/file"), 8, 0, 0, "test")
    (src.path / "folder").rename(src.path / "old_folder")
    (src.path / "folder").symlink_to(protected, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        execute_move(move)
    assert (protected / "file").read_text() == "secret"
    assert not (dst.path / "folder").exists()


def test_cli_accounting_and_config_contract(tmp_path, monkeypatch, capsys):
    ssd1, ssd2, hdd = (branch(tmp_path / name) for name in ("ssd1", "ssd2", "hdd"))
    hdd.rotational = True
    (ssd1.path / ".pbs").mkdir()
    (ssd1.path / ".pbs" / "chunk").write_text("chunk")
    destination = tmp_path / "good.json"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'excluded_paths = [".pbs"]\naccounting_path = "{destination}"\n')
    monkeypatch.setattr("nas_mover.cli.discover_runtime_pool", lambda mount: Pool(mount, [], {}, 0))
    monkeypatch.setattr("nas_mover.cli.discover_branches", lambda pool: [ssd1, ssd2, hdd])
    assert run(["--config", str(config_file), "--lock", str(tmp_path / "lock"), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["excluded_accounting"]["excluded_paths"][0]["files"] == 1
    assert json.loads(destination.read_text()) == result["excluded_accounting"]


def test_failed_atomic_replace_removes_temporary_and_keeps_previous(tmp_path, monkeypatch):
    from nas_mover import accounting
    destination = tmp_path / "snapshot.json"
    destination.write_text("previous")
    def fail(*args):
        raise OSError("replace failed")
    monkeypatch.setattr(accounting.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        publish_snapshot({"complete": True}, destination)
    assert destination.read_text() == "previous"
    assert list(tmp_path.glob(".nas-mover.*")) == []


def test_snapshot_open_failure_leaves_previous(tmp_path, monkeypatch):
    from nas_mover import accounting
    destination = tmp_path / "snapshot.json"
    destination.write_text("previous")
    monkeypatch.setattr(accounting.tempfile, "NamedTemporaryFile", lambda **kw: (_ for _ in ()).throw(OSError("open failed")))
    with pytest.raises(OSError, match="open failed"):
        publish_snapshot({"complete": True}, destination)
    assert destination.read_text() == "previous"


def test_unexpected_executor_exception_restores_signal_handlers(tmp_path, monkeypatch):
    ssd1, ssd2, hdd = (branch(tmp_path / name) for name in ("one", "two", "three"))
    hdd.rotational = True
    monkeypatch.setattr("nas_mover.cli.discover_runtime_pool", lambda mount: Pool(mount, [], {}, 0))
    monkeypatch.setattr("nas_mover.cli.discover_branches", lambda pool: [ssd1, ssd2, hdd])
    monkeypatch.setattr("nas_mover.cli.execute_moves", lambda *args, **kwargs: (_ for _ in ()).throw(TypeError("unexpected")))
    with pytest.raises(TypeError, match="unexpected"):
        run(["--live", "--lock", str(tmp_path / "lock")])


@pytest.mark.parametrize("invalid", ["/absolute", "../outside", "."])
def test_invalid_exclusion(invalid):
    with pytest.raises(ValueError):
        MoverConfig(excluded_paths=(Path(invalid),)).validate()
