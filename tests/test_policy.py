from pathlib import Path

import pytest

from nas_mover import Branch, PoolConfig, choose_destination, plan_moves
from nas_mover.core import scan_files


def branch(
    tmp_path: Path,
    name: str,
    *,
    used: int,
    free: int,
    total: int = 100,
    rotational: bool = False,
) -> Branch:
    path = tmp_path / name
    path.mkdir()
    return Branch(path, 0, rotational, total, free, used)


def test_existing_path_policy_only_uses_matching_directory(tmp_path: Path) -> None:
    first = branch(tmp_path, "first", used=10, free=90)
    second = branch(tmp_path, "second", used=20, free=80)
    (second.path / "movies").mkdir()

    assert (
        choose_destination("eplfs", [first, second], Path("movies/a.mkv"), 1) is second
    )
    assert choose_destination("epff", [first], Path("movies/a.mkv"), 1) is None


def test_lfs_and_lup_are_deterministic(tmp_path: Path) -> None:
    least_free = branch(tmp_path, "least-free", used=20, free=80)
    least_used_percent = branch(tmp_path, "least-used", used=10, free=40, total=50)
    assert (
        choose_destination("lfs", [least_free, least_used_percent], Path("a"), 1)
        is least_used_percent
    )
    assert (
        choose_destination("lup", [least_free, least_used_percent], Path("a"), 1)
        is least_free
    )


def test_policy_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        choose_destination(
            "bogus", [branch(tmp_path, "x", used=1, free=99)], Path("a"), 1
        )  # type: ignore[arg-type]


def test_policy_returns_none_without_space(tmp_path: Path) -> None:
    full = branch(tmp_path, "full", used=100, free=0)
    assert choose_destination("ff", [full], Path("a"), 1) is None


def test_msp_policy_returns_none_without_eligible_candidates(tmp_path: Path) -> None:
    full = branch(tmp_path, "full-msp", used=100, free=0)
    assert choose_destination("msplfs", [full], Path("a"), 1) is None


def test_policy_reports_missing_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nas_mover.policy as policy_module

    monkeypatch.setattr(policy_module, "SUPPORTED_POLICIES", {"unhandled"})
    with pytest.raises(ValueError, match="handler"):
        choose_destination(
            "unhandled", [branch(tmp_path, "handler", used=1, free=99)], Path("a"), 1
        )  # type: ignore[arg-type]


def test_scan_files_ignores_mover_temporaries(tmp_path: Path) -> None:
    storage = branch(tmp_path, "storage", used=1, free=99)
    (storage.path / "keep").write_bytes(b"ok")
    (storage.path / ".nas-mover.partial").write_bytes(b"ignore")
    (storage.path / ".nas-mover.dir").mkdir()
    (storage.path / ".nas-mover.dir" / "nested").write_bytes(b"ignore")
    assert [item.relative_path for item in scan_files(storage)] == [Path("keep")]


def test_scan_files_scope_excludes_other_branch_data(tmp_path: Path) -> None:
    storage = branch(tmp_path, "storage", used=1, free=99)
    (storage.path / "mover-test").mkdir()
    (storage.path / "mover-test" / "inside.bin").write_bytes(b"inside")
    (storage.path / "production.bin").write_bytes(b"outside")
    assert [item.relative_path for item in scan_files(storage, Path("mover-test"))] == [
        Path("mover-test/inside.bin")
    ]
    with pytest.raises(ValueError, match="inside"):
        scan_files(storage, Path(".."))


def test_scan_files_skips_unreadable_and_nonfile_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = branch(tmp_path, "unreadable", used=1, free=99)
    bad = storage.path / "bad.bin"
    not_file = storage.path / "not-file.bin"
    bad.write_bytes(b"bad")
    not_file.write_bytes(b"not a file")
    original_stat = Path.stat
    original_is_file = Path.is_file

    def stat(path: Path, *args, **kwargs):
        if path == bad:
            raise OSError("unreadable")
        return original_stat(path, *args, **kwargs)

    def is_file(path: Path):
        return False if path == not_file else original_is_file(path)

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "is_file", is_file)
    assert scan_files(storage) == []


@pytest.mark.parametrize("policy", ["all", "mfs", "lus", "rand", "pfrd", "newest"])
def test_policy_families_select_a_usable_branch(tmp_path: Path, policy: str) -> None:
    first = branch(tmp_path, "first", used=10, free=90)
    second = branch(tmp_path, "second", used=20, free=80)
    assert choose_destination(policy, [first, second], Path("a"), 1) in [first, second]  # type: ignore[arg-type]


def test_most_shared_path_prefers_deepest_existing_parent(tmp_path: Path) -> None:
    shallow = branch(tmp_path, "shallow", used=10, free=90)
    deep = branch(tmp_path, "deep", used=20, free=80)
    (shallow.path / "media").mkdir()
    (deep.path / "media" / "movies").mkdir(parents=True)
    assert (
        choose_destination("msplfs", [shallow, deep], Path("media/movies/a"), 1) is deep
    )


def test_branch_percentages_handle_empty_filesystem(tmp_path: Path) -> None:
    empty = branch(tmp_path, "empty", used=0, free=0, total=0)
    assert empty.kind == "SSD"
    assert empty.used_percent == 0
    assert empty.simulated_used_percent == 0


def test_newest_policy_uses_newest_existing_parent(tmp_path: Path) -> None:
    old = branch(tmp_path, "old", used=10, free=90)
    new = branch(tmp_path, "new", used=20, free=80)
    (old.path / "media").mkdir()
    (new.path / "media").mkdir()
    old_time = 1_000_000
    new_time = 2_000_000
    import os

    os.utime(old.path / "media", (old_time, old_time))
    os.utime(new.path / "media", (new_time, new_time))
    assert choose_destination("newest", [old, new], Path("media/file"), 1) is new


def test_planner_handles_empty_ssd_set(tmp_path: Path) -> None:
    assert (
        plan_moves(
            [], [], PoolConfig(), watermark_percent=80, tolerance_percent=2, policy="ff"
        )
        == []
    )


def test_planner_stops_when_source_has_no_candidates(tmp_path: Path) -> None:
    source = branch(tmp_path, "source", used=90, free=10)
    other = branch(tmp_path, "other", used=80, free=20)
    assert (
        plan_moves(
            [source, other],
            [],
            PoolConfig(),
            watermark_percent=80,
            tolerance_percent=2,
            policy="ff",
        )
        == []
    )


def test_planner_stops_hdd_phase_when_nothing_exceeds_watermark(tmp_path: Path) -> None:
    first = branch(tmp_path, "below-first", used=10, free=90)
    second = branch(tmp_path, "below-second", used=20, free=80)
    hdd = branch(tmp_path, "below-hdd", used=0, free=100, rotational=True)
    assert (
        plan_moves(
            [first, second],
            [hdd],
            PoolConfig(),
            watermark_percent=80,
            tolerance_percent=2,
            policy="ff",
        )
        == []
    )


def test_planner_stops_when_fullest_source_is_at_watermark(tmp_path: Path) -> None:
    first = branch(tmp_path, "at-first", used=80, free=20)
    second = branch(tmp_path, "at-second", used=70, free=30)
    hdd = branch(tmp_path, "at-hdd", used=0, free=100, rotational=True)
    assert (
        plan_moves(
            [first, second],
            [hdd],
            PoolConfig(),
            watermark_percent=80,
            tolerance_percent=2,
            policy="ff",
        )
        == []
    )


def test_planner_stops_hdd_phase_when_source_is_below_test_watermark(
    tmp_path: Path,
) -> None:
    first = branch(tmp_path, "below-test-first", used=80, free=20)
    second = branch(tmp_path, "below-test-second", used=70, free=30)
    hdd = branch(tmp_path, "below-test-hdd", used=0, free=100, rotational=True)
    assert (
        plan_moves(
            [first, second],
            [hdd],
            PoolConfig(),
            watermark_percent=100,
            tolerance_percent=40,
            policy="ff",
        )
        == []
    )


def test_planner_skips_existing_ssd_destination(tmp_path: Path) -> None:
    source = branch(tmp_path, "existing-source", used=90, free=10)
    destination = branch(tmp_path, "existing-destination", used=20, free=80)
    hdd = branch(tmp_path, "existing-hdd", used=0, free=100, rotational=True)
    (source.path / "file.bin").write_bytes(b"data")
    (destination.path / "file.bin").write_bytes(b"already there")
    assert (
        plan_moves(
            [source, destination],
            [hdd],
            PoolConfig(),
            watermark_percent=80,
            tolerance_percent=2,
            policy="ff",
        )
        == []
    )


def test_planner_balances_ssd_before_spilling_to_hdd(tmp_path: Path) -> None:
    full = branch(tmp_path, "ssd-full", used=90, free=10)
    low = branch(tmp_path, "ssd-low", used=20, free=80)
    hdd = branch(tmp_path, "hdd", used=0, free=100, rotational=True)
    (full.path / "old.bin").write_bytes(b"1234567890")
    (full.path / "new.bin").write_bytes(b"12")

    moves = plan_moves(
        [full, low],
        [hdd],
        PoolConfig(),
        watermark_percent=80,
        tolerance_percent=2,
        policy="ff",
    )

    assert moves[0].reason == "SSD -> SSD"
    assert all(move.destination_branch is not hdd for move in moves[:1])


def test_planner_returns_no_move_when_no_destination_space(tmp_path: Path) -> None:
    source = branch(tmp_path, "source", used=90, free=10)
    other = branch(tmp_path, "other", used=90, free=10)
    hdd = branch(tmp_path, "hdd", used=99, free=1, rotational=True)
    (source.path / "large.bin").write_bytes(b"1234567890")
    assert (
        plan_moves(
            [source, other],
            [hdd],
            PoolConfig(),
            watermark_percent=80,
            tolerance_percent=2,
            policy="ff",
        )
        == []
    )


def test_planner_spills_to_hdd_after_ssds_reach_tolerance(tmp_path: Path) -> None:
    source = branch(tmp_path, "source", used=90, free=10)
    other = branch(tmp_path, "other", used=80, free=20)
    hdd = branch(tmp_path, "hdd", used=0, free=100, rotational=True)
    (source.path / "file.bin").write_bytes(b"12")
    moves = plan_moves(
        [source, other],
        [hdd],
        PoolConfig(),
        watermark_percent=80,
        tolerance_percent=2,
        policy="ff",
    )
    assert moves and moves[0].destination_branch is hdd


def test_planner_tries_another_ssd_when_fullest_has_no_scoped_files(
    tmp_path: Path,
) -> None:
    empty_full = branch(tmp_path, "empty-full", used=90, free=10)
    source = branch(tmp_path, "source", used=80, free=20)
    hdd = branch(tmp_path, "hdd", used=0, free=100, rotational=True)
    (source.path / "mover-test").mkdir()
    (source.path / "mover-test" / "file.bin").write_bytes(b"12")
    moves = plan_moves(
        [empty_full, source],
        [hdd],
        PoolConfig(),
        watermark_percent=0,
        tolerance_percent=0,
        policy="ff",
        scope=Path("mover-test"),
    )
    assert moves and moves[0].source_branch is source
