# nas-mover

`nas-mover` plans and executes file moves between branches of a live mergerfs pool. It is intended for a NAS with fast SSD landing branches and larger HDD data branches.

It does **not** own routine maintenance scheduling, UPS policy, SnapRAID, parity lifecycle, filesystem mounting/unlocking, enclosure power, or Stage 1 outage orchestration. Those integration responsibilities belong to `nas-config` under the accepted homelab ADRs.

## Ownership boundary

```text
                nas-config
                   |
         bounded routine window
         AC/lifecycle admission
         cooperative stop deadline
                   |
                   v
               nas-mover
        plan + transfer files only
                   |
          +--------+--------+
          |        |        |
          v        v        v
        hdd1     hdd3     hdd4 ...
      serial    serial    serial
       queue     queue     queue

Distinct destination HDD queues may execute concurrently.
Moves to the same destination HDD remain serialized.
```

`nas-config` may run the mover for a bounded routine window or invoke it manually for a longer/full drain. `nas-mover` itself should remain useful independently of that scheduler.

## Current execution

The executor serializes SSD-destination moves, then runs one serial queue per
HDD destination concurrently. Planner-selected paths remain authoritative.
Cancellation and failure stop new work and join all workers before releasing
the process lock.

## Planning behavior

The planner works in two phases:

1. If one SSD is above the watermark and another is materially below it, move cold files from the fuller SSD to the less-full SSD.
2. Once every SSD is within the configured tolerance of the watermark, move excess SSD files to eligible HDD branches.

Production defaults:

| Setting | Default |
| --- | --- |
| SSD watermark | `80%` |
| Watermark tolerance | `2%` |
| HDD destination policy | `eplfs` |
| Verification | size + source stability |
| Lock file | `/run/lock/nas-mover.lock` |

`eplfs` means existing path, least free space. The destination must have the same parent directory and enough free space after honoring mergerfs `minfreespace` and the configured extra reserve.

Planning updates simulated branch capacity after every planned move. Parallel execution must preserve that already-decided plan rather than making fresh destination decisions at runtime.

## Runtime discovery

Production discovery is based on the mounted mergerfs pool, not `/etc/fstab`.

The mover:

- validates the selected mount is `fuse.mergerfs`;
- reads active branches from `user.mergerfs.branches` on the `.mergerfs` control file;
- reads active `minfreespace` from mergerfs runtime metadata;
- uses `findmnt` and `lsblk` to classify active branches as SSD (`ROTA=0`) or HDD (`ROTA=1`);
- fails closed if required runtime topology/reserve information cannot be established.

Typical production pool:

```text
/mnt/nas/ssd1-data/data
/mnt/nas/ssd2-data/data
/mnt/nas/hdd1-data/data
/mnt/nas/hdd3-data/data
/mnt/nas/hdd4-data/data
/mnt/nas/hdd6-data/data
```

Parity filesystems are not mergerfs data branches. `nas-mover` never mounts, unlocks, repairs, spins down, or powers storage devices.

`--fstab` remains a compatibility/testing path only.

## Transactional move safety

For every file, live mode performs:

```text
validate source + destination
-> copy to .nas-mover.<name>.<pid>.partial
-> verify source did not change
-> verify destination size (or SHA-256 when requested)
-> atomic rename partial -> final destination
-> fsync destination directory metadata
-> delete source
```

The source is deleted only after the destination is complete and committed.

### Cooperative cancellation

SIGTERM/SIGINT request cancellation. Active copies check cancellation between
chunks, clean their partial files, and preserve their sources. Previously
completed moves remain committed. All workers quiesce before the process lock
releases and exit 130 is returned.

## Structured execution reporting

Use `--json` for one terminal result, or `--json-events` for ephemeral JSON Lines
planned/progress/result events. Both use schema version 1. Default human output
summarizes planned and completed file/byte counts by branch route.
See [the execution contract](docs/reporting.md) for the exact schema, accounting,
progress, error/exit semantics, crash limitations and maintenance integration.
Progress lives in memory; the caller owns durable terminal-result retention.

## Dry-run and live operation

Dry-run is the default:

```bash
sudo /opt/nas-mover/.venv/bin/nas-mover \
  --config /etc/nas-mover/config.toml
```

Apply the plan only with `--live`:

```bash
sudo /opt/nas-mover/.venv/bin/nas-mover \
  --config /etc/nas-mover/config.toml \
  --live
```

`--scope` restricts planning to a relative directory and is primarily useful for controlled integration testing. Absolute paths and `..` traversal are rejected.

## Configuration

Host-specific mover policy is loaded from `/etc/nas-mover/config.toml`. In the homelab deployment this runtime file may be rendered/installed by `nas-config`; it is not the authority for the routine maintenance clock or mover deadline.

The routine maintenance start time, mover-duration window, settle interval, and SnapRAID scrub policy live in `nas-config/config/nas-system.toml`, not this project.

## Testing requirements

Repository tests must continue to cover the transactional move path and planner behavior. Destination concurrency coverage includes:

1. multiple planned moves to one HDD remain serialized;
2. moves to two or more distinct HDD destinations overlap in execution;
3. planner-selected destination paths are preserved exactly;
4. destination reserve/capacity planning remains valid;
5. failure of one worker stops new work and produces a deterministic overall result;
6. SIGTERM/SIGINT with one worker active removes its partial and retains its source;
7. SIGTERM/SIGINT with multiple destination workers waits for every worker to quiesce;
8. already completed moves remain committed after cancellation;
9. process lock releases only after workers are done.

Live validation should use a dedicated scoped test directory containing generated fixtures only.

## Development map

```text
src/nas_mover/models.py       domain structures
src/nas_mover/policy.py       destination policy
src/nas_mover/planner.py      scanning and planning
src/nas_mover/transfer.py     transactional file transfer
src/nas_mover/discovery.py    live mergerfs/device discovery
src/nas_mover/locking.py      process lock
src/nas_mover/config.py       mover-specific config
src/nas_mover/cli.py          command orchestration
src/nas_mover/test_suite.py   NAS integration harness
```

Run tests with:

```bash
python -m pytest
```

The project maintains a 100% meaningful statement/branch coverage gate for measured mover logic.

## Architecture principle

Keep this project narrow. `nas-mover` should become better at safely and efficiently executing its already-planned file moves, but it should not grow into a NAS maintenance daemon. Scheduling, power/outage decisions, SnapRAID sequencing, and storage lifecycle belong outside it.