from pathlib import Path
from types import SimpleNamespace

import pytest

from nas_mover.discovery import Pool, discover_runtime_pool


def test_runtime_pool_reads_minfreespace_xattr_when_findmnt_omits_it(tmp_path: Path) -> None:
    mount = tmp_path / "pool"
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0, stdout="")
        if command[0] == "findmnt" and command[3] == "FSTYPE":
            return SimpleNamespace(returncode=0, stdout="fuse.mergerfs\n")
        if command[0] == "findmnt" and command[3] == "OPTIONS":
            return SimpleNamespace(returncode=0, stdout="rw,relatime,allow_other\n")
        if command[0] == "getfattr" and "user.mergerfs.branches" in command:
            return SimpleNamespace(returncode=0, stdout="/ssd1=RW:/ssd2=RW:/hdd=RW\n")
        if command[0] == "getfattr" and "user.mergerfs.minfreespace" in command:
            return SimpleNamespace(returncode=0, stdout="21474836480\n")
        raise AssertionError(command)

    pool = discover_runtime_pool(mount, runner)
    assert pool == Pool(
        mount,
        [Path("/ssd1"), Path("/ssd2"), Path("/hdd")],
        {"rw": True, "relatime": True, "allow_other": True, "minfreespace": "21474836480"},
        20 * 1024**3,
    )
    assert any("user.mergerfs.minfreespace" in command for command in calls)


@pytest.mark.parametrize("value", ["", "bogus", "-1"])
def test_runtime_pool_rejects_invalid_minfreespace_xattr(tmp_path: Path, value: str) -> None:
    mount = tmp_path / "pool"

    def runner(command, **kwargs):
        if command[0] == "mountpoint":
            return SimpleNamespace(returncode=0, stdout="")
        if command[0] == "findmnt" and command[3] == "FSTYPE":
            return SimpleNamespace(returncode=0, stdout="fuse.mergerfs\n")
        if command[0] == "findmnt" and command[3] == "OPTIONS":
            return SimpleNamespace(returncode=0, stdout="rw,allow_other\n")
        if command[0] == "getfattr" and "user.mergerfs.branches" in command:
            return SimpleNamespace(returncode=0, stdout="/ssd1=RW:/ssd2=RW:/hdd=RW\n")
        if command[0] == "getfattr" and "user.mergerfs.minfreespace" in command:
            return SimpleNamespace(returncode=0, stdout=value)
        raise AssertionError(command)

    with pytest.raises(RuntimeError, match="minfreespace"):
        discover_runtime_pool(mount, runner)
