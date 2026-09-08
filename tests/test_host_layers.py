from pathlib import Path
from types import SimpleNamespace

import pytest

from nas_mover import Branch, PlannedMove
from nas_mover.cli import main, run
from nas_mover.config import MoverConfig, parse_size
from nas_mover.discovery import (
    Pool,
    backing_source,
    discover_branches,
    discover_runtime_pool,
    parse_fstab,
    require_mount,
    rotational_for_path,
    stat_branch,
)
from nas_mover.sandbox import Sandbox
from nas_mover.locking import process_lock


def test_config_uses_production_defaults_and_validates() -> None:
    config = MoverConfig()
    config.validate()
    assert (config.watermark_percent, config.tolerance_percent) == (80.0, 2.0)
    assert config.mount_override is None
    assert parse_size("20G") == 20 * 1024**3
    with pytest.raises(ValueError, match="Invalid size"):
        parse_size("not-a-size")


def test_config_rejects_unsafe_values() -> None:
    with pytest.raises(ValueError, match="watermark"):
        MoverConfig(watermark_percent=101).validate()
    with pytest.raises(ValueError, match="verification"):
        MoverConfig(verification="md5").validate()
    with pytest.raises(ValueError, match="tolerance"):
        MoverConfig(tolerance_percent=-1).validate()
    with pytest.raises(ValueError, match="policy"):
        MoverConfig(policy="bogus").validate()  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="file_age"):
        MoverConfig(min_file_age_hours=-1).validate()
    with pytest.raises(ValueError, match="extra_free"):
        MoverConfig(extra_free_percent=-1).validate()


def test_config_loads_toml_and_converts_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        'watermark_percent = 75.0\n'
        'tolerance_percent = 3.0\n'
        'policy = "ff"\n'
        'verification = "sha256"\n'
        'fstab_path = "/tmp/test-fstab"\n'
        'mount_override = "/tmp/test-pool"\n'
        'lock_path = "/tmp/test.lock"\n'
    )
    config = MoverConfig.from_file(config_file)
    assert config.watermark_percent == 75.0
    assert config.policy == "ff"
    assert config.fstab_path == Path("/tmp/test-fstab")
    assert config.mount_override == Path("/tmp/test-pool")
    assert config.lock_path == Path("/tmp/test.lock")


def test_config_rejects_unknown_keys_and_missing_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        MoverConfig.from_file(tmp_path / "missing.toml")
    config_file = tmp_path / "unknown.toml"
    config_file.write_text("watermark_percent = 80\nwatremark_percent = 80\n")
    with pytest.raises(ValueError, match="Unknown"):
        MoverConfig.from_file(config_file)
    minimal = tmp_path / "minimal.toml"
    minimal.write_text("watermark_percent = 80\n")
    assert MoverConfig.from_file(minimal).lock_path == Path("/run/lock/nas-mover.lock")


def test_sandbox_rejects_root_and_escape_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        Sandbox(Path(tmp_path.anchor))
    sandbox = Sandbox(tmp_path / "sandbox")
    with pytest.raises(ValueError, match="escapes"):
        sandbox.path(Path("..") / "outside")
    with pytest.raises(ValueError, match="current working"):
        Sandbox(Path.cwd())


def test_sandbox_creates_and_removes_only_named_fixtures(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path / "sandbox")
    fixtures = sandbox.create_fixtures(2)
    keep = sandbox.create_fixture(Path("keep.txt"), b"keep")
    assert all(sandbox.path(relative).is_file() for relative in fixtures)
    sandbox.remove_fixtures(fixtures)
    assert not sandbox.path(fixtures[0]).exists()
    assert keep.exists()
    sandbox.remove_fixture(Path("missing.txt"))
    with pytest.raises(ValueError, match="positive"):
        sandbox.fixture_paths(0)


def test_parse_fstab_selects_mergerfs_and_reads_reserve(tmp_path: Path) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text(
        "# ignored\n"
        "/ssd1:/ssd2:/hdd /pool fuse.mergerfs defaults,minfreespace=20G 0 0\n"
    )
    pool = parse_fstab(fstab, Path("/pool"))
    assert pool == Pool(
        Path("/pool"), [Path("/ssd1"), Path("/ssd2"), Path("/hdd")],
        {"defaults": True, "minfreespace": "20G"}, 20 * 1024**3,
    )
    assert parse_fstab(fstab).mountpoint == Path("/pool")


def test_parse_fstab_rejects_missing_or_ambiguous_pool(tmp_path: Path) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text("/dev/sda /data ext4 defaults 0 0\n")
    with pytest.raises(RuntimeError, match="No matching"):
        parse_fstab(fstab)
    fstab.write_text(
        "/a:/b /one fuse.mergerfs defaults 0 0\n"
        "/c:/d /two fuse.mergerfs defaults 0 0\n"
    )
    with pytest.raises(RuntimeError, match="More than one"):
        parse_fstab(fstab, None)
    fstab.write_text('"unterminated /pool fuse.mergerfs defaults 0 0\n')
    with pytest.raises(RuntimeError, match="No matching"):
        parse_fstab(fstab)


def test_runtime_pool_uses_active_mergerfs_branches_and_options(tmp_path: Path) -> None:
    mount = tmp_path / "pool"
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0, stdout="")
        if command[0] == "getfattr":
            return SimpleNamespace(returncode=0, stdout="/ssd1=RW:/ssd2=RW:/hdd=RW\n")
        if command[3] == "FSTYPE":
            return SimpleNamespace(returncode=0, stdout="fuse.mergerfs\n")
        if command[3] == "OPTIONS":
            return SimpleNamespace(returncode=0, stdout="rw,minfreespace=20G,category.create=ff\n")
        raise AssertionError(command)

    pool = discover_runtime_pool(mount, runner)
    assert pool == Pool(
        mount, [Path("/ssd1"), Path("/ssd2"), Path("/hdd")],
        {"rw": True, "minfreespace": "20G", "category.create": "ff"}, 20 * 1024**3,
    )
    assert [command[0] for command in calls] == ["mountpoint", "findmnt", "getfattr", "findmnt"]


def test_runtime_pool_rejects_non_mergerfs_and_bad_branches(tmp_path: Path) -> None:
    mount = tmp_path / "pool"

    def non_mergerfs(command, **kwargs):
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="ext4\n")

    with pytest.raises(RuntimeError, match="not mergerfs"):
        discover_runtime_pool(mount, non_mergerfs)

    def bad_branch(command, **kwargs):
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0, stdout="")
        if command[0] == "getfattr":
            return SimpleNamespace(returncode=0, stdout="/ssd=RO\n")
        if command[3] == "FSTYPE":
            return SimpleNamespace(returncode=0, stdout="fuse.mergerfs\n")
        return SimpleNamespace(returncode=0, stdout="rw,minfreespace=20G\n")

    with pytest.raises(RuntimeError, match="Unexpected mergerfs"):
        discover_runtime_pool(mount, bad_branch)


def test_discovery_checks_mounts_devices_and_capacity(tmp_path: Path) -> None:
    def runner(*args, **kwargs):
        command = args[0]
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0, stdout="")
        if command[0] == "findmnt":
            device = "/dev/ssd" if command[-1].endswith("ssd") else "/dev/hdd"
            return SimpleNamespace(returncode=0, stdout=f"{device}\n")
        device_type = "0" if command[-1] == "/dev/ssd" else "1"
        return SimpleNamespace(returncode=0, stdout=f"{device_type}\n")

    pool = Pool(tmp_path, [tmp_path / "ssd", tmp_path / "hdd"], {}, 0)
    info = SimpleNamespace(f_frsize=10, f_bsize=10, f_blocks=100, f_bavail=40, f_bfree=20)
    import nas_mover.discovery as discovery
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(discovery.os, "statvfs", lambda path: info, raising=False)
    try:
        branches = discover_branches(pool, runner)
    finally:
        monkeypatch.undo()
    assert [branch.rotational for branch in branches] == [False, True]
    assert branches[0].total_bytes == 1000


def test_discovery_rejects_mount_and_device_errors(tmp_path: Path) -> None:
    failed = lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="")
    with pytest.raises(RuntimeError, match="not mounted"):
        require_mount(tmp_path, failed)
    blank = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="")
    with pytest.raises(RuntimeError, match="backing device"):
        backing_source(tmp_path, blank)
    invalid = lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="2")
    with pytest.raises(RuntimeError, match="rotational"):
        rotational_for_path(tmp_path, invalid)


def test_cli_is_dry_run_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text("/ssd1:/ssd2:/hdd /pool fuse.mergerfs defaults 0 0\n")
    branches = [
        Branch(tmp_path / "ssd1", 0, False, 100, 10, 90),
        Branch(tmp_path / "ssd2", 1, False, 100, 20, 80),
        Branch(tmp_path / "hdd", 2, True, 100, 100, 0),
    ]
    move = PlannedMove(branches[0], branches[2], Path("file"), 1, 0, 0, "test")
    monkeypatch.setattr("nas_mover.cli.discover_branches", lambda pool: branches)
    monkeypatch.setattr("nas_mover.cli.plan_moves", lambda *args, **kwargs: [move])
    execute = SimpleNamespace(called=False)
    monkeypatch.setattr("nas_mover.cli.execute_move", lambda *args, **kwargs: setattr(execute, "called", True))

    lock = tmp_path / "nas-mover.lock"
    assert run(["--fstab", str(fstab), "--mount", "/pool", "--lock", str(lock)]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert not execute.called

    assert run(["--live", "--fstab", str(fstab), "--mount", "/pool", "--lock", str(lock)]) == 0
    assert execute.called


def test_cli_uses_runtime_pool_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    branches = [
        Branch(tmp_path / "ssd1", 0, False, 100, 100, 0),
        Branch(tmp_path / "ssd2", 1, False, 100, 100, 0),
        Branch(tmp_path / "hdd", 2, True, 100, 100, 0),
    ]
    seen = SimpleNamespace(mount=None)
    pool = Pool(Path("/mnt/nas/data"), [branch.path for branch in branches], {}, 0)
    monkeypatch.setattr("nas_mover.cli.discover_runtime_pool", lambda mount: (setattr(seen, "mount", mount), pool)[1])
    monkeypatch.setattr("nas_mover.cli.discover_branches", lambda value: branches)
    monkeypatch.setattr("nas_mover.cli.plan_moves", lambda *args, **kwargs: [])
    assert run(["--lock", str(tmp_path / "lock")]) == 0
    assert seen.mount == Path("/mnt/nas/data")


def test_cli_rejects_escaping_scope(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        run(["--scope", "../outside"])


def test_cli_rejects_insufficient_ssds_and_missing_hdd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fstab = tmp_path / "fstab"
    fstab.write_text("/pool /pool fuse.mergerfs defaults 0 0\n")
    one_ssd = [Branch(tmp_path / "one", 0, False, 100, 90, 10)]
    monkeypatch.setattr("nas_mover.cli.discover_branches", lambda pool: one_ssd)
    with pytest.raises(RuntimeError, match="two SSD"):
        run(["--fstab", str(fstab), "--mount", "/pool", "--lock", str(tmp_path / "lock")])
    two_ssds = one_ssd + [Branch(tmp_path / "two", 1, False, 100, 90, 10)]
    monkeypatch.setattr("nas_mover.cli.discover_branches", lambda pool: two_ssds)
    with pytest.raises(RuntimeError, match="No HDD"):
        run(["--fstab", str(fstab), "--mount", "/pool", "--lock", str(tmp_path / "lock")])


def test_cli_main_reports_runtime_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("nas_mover.cli.run", lambda: (_ for _ in ()).throw(RuntimeError("expected")))
    assert main() == 1
    assert "ERROR: expected" in capsys.readouterr().out


def test_process_lock_is_noop_on_non_posix(tmp_path: Path) -> None:
    with process_lock(tmp_path / "lock", platform_name="nt"):
        pass


def test_process_lock_uses_flock_on_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import nas_mover.locking as locking

    calls: list[tuple[int, int]] = []

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 0

        @staticmethod
        def flock(fd: int, flags: int) -> None:
            calls.append((fd, flags))

    monkeypatch.setitem(sys.modules, "fcntl", FakeFcntl)
    lock_path = tmp_path / "nested" / "lock"
    with process_lock(lock_path, platform_name="posix"):
        assert lock_path.read_text().isdigit()
    assert calls[0][1] == FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB
    assert calls[1][1] == 0


def test_process_lock_reports_contention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import nas_mover.locking as locking

    class BusyFcntl:
        LOCK_EX = 1
        LOCK_NB = 2
        LOCK_UN = 0

        @staticmethod
        def flock(fd: int, flags: int) -> None:
            if flags != BusyFcntl.LOCK_UN:
                raise BlockingIOError

    monkeypatch.setitem(sys.modules, "fcntl", BusyFcntl)
    with pytest.raises(RuntimeError, match="already running"):
        with process_lock(tmp_path / "lock", platform_name="posix"):
            pass
