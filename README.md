# nas-mover

`nas-mover` is a Linux command-line tool for moving files between tiers in a
mergerfs pool. It is intended for a NAS with fast SSD landing/cache branches
and larger HDD data branches.

The mover does not replace mergerfs and does not run SnapRAID. mergerfs still
provides the unified mount and its normal file-placement behavior. This tool
looks at the individual branches, plans selected moves, and performs them only
after a copy and verification succeed.

## How It Works

### Mergerfs terms

- A **branch** is one directory or mounted filesystem supplied to mergerfs.
- The **pool mountpoint** is the unified directory users access, such as
  `/mnt/nas/data`.
- A **policy** decides which eligible branch receives a destination file.
- `minfreespace` reserves space on a branch; the mover reads the active value
  from mergerfs runtime control metadata.
- The mover uses `findmnt` and `lsblk` to identify whether each branch is SSD
  (`ROTA=0`) or HDD (`ROTA=1`).

### Planning behavior

The planner works in two phases:

1. If one SSD is above the watermark and another is materially below it, move
   cold files from the fuller SSD to the less-full SSD.
2. Once every SSD is within the configured tolerance of the watermark, move
   excess SSD files to eligible HDD branches.

The production defaults are:

| Setting | Default |
| --- | --- |
| SSD watermark | `80%` |
| Watermark tolerance | `2%` |
| HDD destination policy | `eplfs` |
| Verification | Size and source stability |
| Lock file | `/run/lock/nas-mover.lock` |

`eplfs` means “existing path, least free space.” The destination must have the
same parent directory and enough free space after honoring `minfreespace`.

### Live move safety

For each planned file, live mode:

1. Refuses a missing source or existing destination.
2. Copies to a hidden temporary file in the destination directory.
3. Confirms the source size and modification time did not change.
4. Verifies destination size, or SHA-256 when the integration harness requests
   it.
5. Atomically renames the temporary file into place.
6. Flushes destination directory metadata on POSIX systems.
7. Deletes the source only after all previous steps succeed.

Normal `nas-mover` execution is dry-run by default. `--live` is required to
change files.

## Configure Your NAS

Production discovery is runtime-based. The selected mergerfs pool must already
be mounted. `nas-mover` validates that the target is a `fuse.mergerfs` mount,
then reads the current branch list from `user.mergerfs.branches` on the pool's
`.mergerfs` control file. This matters because branch membership may change at
runtime and `/etc/fstab` can be stale or intentionally contain no mergerfs
entry.

The mover also reads the active `minfreespace` reserve from mergerfs runtime
metadata. Some FUSE mounts do not expose mergerfs-specific options through
`findmnt`, so `user.mergerfs.minfreespace` is the authoritative fallback. If
no runtime reserve can be determined, runtime discovery fails closed rather
than silently assuming zero reserve.

For the production NAS, configure the pool explicitly:

```toml
mount_override = "/mnt/nas/data"
```

Inspect the active pool before installing or troubleshooting the mover:

```bash
findmnt -n -o FSTYPE,OPTIONS --target /mnt/nas/data
sudo getfattr -d -m 'user\.mergerfs\..*' -- /mnt/nas/data/.mergerfs
```

The following is an example branch layout, not a required layout:

```text
/mnt/nas/ssd1-data/data
/mnt/nas/ssd2-data/data
/mnt/nas/hdd1-data/data
/mnt/nas/hdd3-data/data
/mnt/nas/hdd4-data/data
/mnt/nas/hdd6-data/data
```

Parity filesystems should not be mergerfs data branches. The program checks
that the selected pool and every active branch are mounted. It does not mount,
unlock, or repair filesystems.

`--fstab` is retained as an explicit compatibility/testing path. It is not the
production discovery source.

## Install On The NAS

Install the release branch or tag you have reviewed in an isolated virtual
environment:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip attr

sudo mkdir -p /opt/nas-mover
sudo chown "$USER:$USER" /opt/nas-mover
git clone https://github.com/jigleski/nas-mover.git /opt/nas-mover

cd /opt/nas-mover
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[test]'
```

The `attr` package provides `getfattr`, which runtime mergerfs discovery uses.

### Host configuration

Copy [config.example.toml](config.example.toml) to `/etc/nas-mover/config.toml`
and edit the host-specific values. The file is TOML, so lines beginning with
`#` are comments. Paths, percentages, policy, age filtering, reserve space,
and verification mode are documented there. The loader rejects unknown keys
and invalid enum values.

```bash
sudo install -d -m 0755 /etc/nas-mover
sudo cp config.example.toml /etc/nas-mover/config.toml
sudoedit /etc/nas-mover/config.toml
```

Run it explicitly. Normal execution remains dry-run:

```bash
sudo /opt/nas-mover/.venv/bin/nas-mover \
  --config /etc/nas-mover/config.toml
```

For a one-off alternate pool, use `--mount`:

```bash
sudo /opt/nas-mover/.venv/bin/nas-mover \
  --config /etc/nas-mover/config.toml \
  --mount /path/to/other/mergerfs/pool
```

CLI flags such as `--fstab`, `--mount`, `--scope`, and `--lock` override the
file for a single run. Keep test-only overrides out of the production config.

For later updates:

```bash
cd /opt/nas-mover
source .venv/bin/activate
git pull --ff-only origin main
python -m pip install -e .
```

## Required End-User Validation

Before relying on the mover, validate it against a dedicated test directory in
your own mergerfs data branches. Do not use a directory containing production
files. Keep the repository's automated tests separate from this live test:

```bash
cd /opt/nas-mover
source .venv/bin/activate
python -m pytest
```

The suite uses real temporary files for transfer behavior and mocks Linux
commands in unit tests. The commands below exercise the actual mounted pool,
branch devices, permissions, and mergerfs runtime layout.

Choose a relative scope that is unused on every data branch, for example
`mover-test/source`. Create that directory on each active data branch. Then run
the one-command validation:

```bash
sudo /opt/nas-mover/.venv/bin/nas-mover-test-suite \
  --config /etc/nas-mover/config.toml \
  --scope mover-test/source \
  --live
```

This command:

- Runs the full pytest suite.
- Discovers the active mergerfs pool, branches, mounts, and SSD/HDD types.
- Creates six named fixture files only in the scoped test directory on the
  first SSD branch.
- Plans only files under that relative scope.
- Prints the six planned moves.
- With `--live`, copies, verifies, hashes, and deletes the fixtures.
- Cleans the named fixture files from every branch even if verification fails.

The harness refuses to overwrite existing `test-*.bin` fixtures. Do not use a
scope containing production files. The `--live` flag is intentionally required.
The harness uses a zero watermark only for these six controlled fixtures; that
does not change the production default of `80%`.

To run the same setup and scoped dry run without moving files, omit `--live`:

```bash
sudo /opt/nas-mover/.venv/bin/nas-mover-test-suite \
  --config /etc/nas-mover/config.toml \
  --scope mover-test/source
```

### Review the result

Confirm that the live command reports successful copy, SHA-256 verification,
source deletion, and cleanup. Independently inspect each branch and confirm
that only the six generated `test-*.bin` files were involved. If any path is
unexpected, stop and investigate before using the mover on production data.

## Normal Operation

Always start with a dry run and review every proposed path:

```bash
sudo /opt/nas-mover/.venv/bin/nas-mover \
  --config /etc/nas-mover/config.toml
```

The `--scope` option is for controlled testing and is relative to every
mergerfs branch; absolute paths and `..` traversal are rejected.

Do not use `--watermark 0 --tolerance 0` for normal operation. Those overrides
exist only to force a controlled test plan with tiny fixture files.

This project does not currently install a systemd service or timer. Scheduling
should be added only after the production dry-run output, logging, alerting,
and SnapRAID sequencing have been designed and reviewed.

## Development And Coverage

The source uses a `src/` layout:

```text
src/nas_mover/models.py       domain data structures
src/nas_mover/policy.py       destination policies
src/nas_mover/planner.py      scanning and move planning
src/nas_mover/transfer.py     copy, verify, replace, delete
src/nas_mover/discovery.py    runtime mergerfs and device discovery
src/nas_mover/locking.py      POSIX process lock
src/nas_mover/config.py       validated defaults and parsing
src/nas_mover/cli.py          dry-run/live mover command
src/nas_mover/test_suite.py   NAS integration harness
```

Run the suite with branch coverage:

```bash
python -m pytest
```

The measured mover logic currently has a 100% statement and branch coverage
gate. The command-entry wrappers are excluded from coverage because they only
delegate into tested functions.

## Command Help

The installed commands provide the following help text.

### `nas-mover --help`

```text
usage: nas-mover [-h] [--live] [--config CONFIG] [--fstab FSTAB]
                 [--mount MOUNT] [--lock LOCK] [--scope SCOPE]
                 [--watermark WATERMARK] [--tolerance TOLERANCE]

Balance mergerfs SSD storage and spill excess to HDD.

options:
  -h, --help            show this help message and exit
  --live                Apply the plan; dry-run is the default.
  --config CONFIG       Path to an editable TOML configuration file.
  --fstab FSTAB         Use fstab compatibility/testing discovery instead of
                        runtime mergerfs discovery.
  --mount MOUNT         Override the configured mergerfs mountpoint.
  --lock LOCK           Override the configured lock path for testing or
                        staging.
  --scope SCOPE         Restrict planning to a relative branch directory.
  --watermark WATERMARK
                        Override the SSD watermark percentage for testing.
  --tolerance TOLERANCE
                        Override the SSD watermark tolerance for testing.
```

### `nas-mover-test-fixtures --help`

```text
usage: nas-mover-test-fixtures [-h] [--count COUNT] [--cleanup] sandbox

Create or remove NAS mover test fixtures in a dedicated sandbox.

positional arguments:
  sandbox        Dedicated sandbox directory; production mounts are refused.

options:
  -h, --help     show this help message and exit
  --count COUNT  Number of test files to create.
  --cleanup      Remove only the named test files.
```

### `nas-mover-test-suite --help`

```text
usage: nas-mover-test-suite [-h] [--config CONFIG] [--fstab FSTAB]
                            [--mount MOUNT] --scope SCOPE [--lock LOCK]
                            [--live]

Run pytest and a scoped NAS mover integration test.

options:
  -h, --help       show this help message and exit
  --config CONFIG  Path to an editable TOML configuration file.
  --fstab FSTAB    Use fstab compatibility/testing discovery.
  --mount MOUNT
  --scope SCOPE    Relative test directory on every branch.
  --lock LOCK
  --live           Apply the scoped integration plan.
```
