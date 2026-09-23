from pathlib import Path
from types import SimpleNamespace

import pytest

from nas_mover.discovery import Pool, discover_branches, discover_runtime_pool, parse_branch, parse_fstab
from nas_mover.models import Branch, PoolConfig
from nas_mover.planner import plan_moves


@pytest.mark.parametrize("text", ["/ssd=RW,garbage", "/ssd=RO,150G", "ssd=RW,150G", "/ssd=RW,1G,2G", "/ssd"])
def test_invalid_branch_reserve_fails_closed(text):
    with pytest.raises(RuntimeError):
        parse_branch(text)


def test_fstab_and_runtime_reserves_are_branch_specific(tmp_path, monkeypatch):
    source = "/ssd1=RW,150G:/ssd2=RW:/hdd=RW"
    fstab = tmp_path / "fstab"
    fstab.write_text(f"{source} /pool fuse.mergerfs minfreespace=20G 0 0\n")
    parsed = parse_fstab(fstab)
    assert parsed.branch_min_free_bytes == {Path("/ssd1"): 150 * 1024**3}

    def runner(command, **kwargs):
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0)
        if command[0] == "getfattr":
            return SimpleNamespace(stdout=source)
        if command[3] == "FSTYPE":
            return SimpleNamespace(stdout="fuse.mergerfs")
        return SimpleNamespace(stdout="rw,minfreespace=20G")

    pool = discover_runtime_pool(Path("/pool"), runner)
    assert pool.branch_min_free_bytes == parsed.branch_min_free_bytes
    monkeypatch.setattr("nas_mover.discovery.require_mount", lambda *a, **kw: None)
    monkeypatch.setattr("nas_mover.discovery.stat_branch", lambda path, order, runner: Branch(path, order, False, 1000, 100, 900))
    assert [branch.min_free_bytes for branch in discover_branches(pool)] == [150 * 1024**3, 0, 0]


def test_ssd_destination_respects_its_branch_reserve(tmp_path):
    full, landing = (Branch(tmp_path / name, i, False, 100, free, 100-free)
                     for i, (name, free) in enumerate((("full", 10), ("landing", 30))))
    for branch in (full, landing):
        branch.path.mkdir()
    (full.path / "file").write_bytes(b"a" * 5)
    landing.min_free_bytes = 28
    assert plan_moves([full, landing], [], PoolConfig(), watermark_percent=80,
                      tolerance_percent=2, policy="ff") == []
    landing.min_free_bytes = 20
    assert len(plan_moves([full, landing], [], PoolConfig(), watermark_percent=80,
                          tolerance_percent=2, policy="ff")) == 1
