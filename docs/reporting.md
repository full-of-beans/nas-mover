# Execution contract v1

Progress is ephemeral; results are durable. `nas-mover --json` writes one terminal
JSON object to stdout. `nas-mover --json-events` writes independently parseable
JSON Lines envelopes `{schema_version: 1, event: KIND, result: RESULT}`. The flags
are mutually exclusive; default operation remains dry-run and `--live` applies
the existing plan. Human mode renders the same terminal model, with aggregated
route rows instead of the former per-file listing.

Events are `planned`, `progress`, and `result`. `planned` follows complete planning;
`progress` follows successful transfer completion, at most once per second;
`result` is the terminal snapshot after all workers have quiesced. There is no
heartbeat and no direct query socket: a parent consumes the stream and retains
its latest snapshot in memory. Short runs may have only one progress event.
Every event contains cumulative counters, so throttling loses no terminal facts.
No mover progress or result files are written. The existing process lock is
unchanged. Callers choose whether to retain the final result durably.

## Exact result fields

| Field | Type / meaning |
| --- | --- |
| schema_version | integer, always 1 |
| mode | `dry_run` or `live` |
| status | `planning`, `running`, `planned`, `completed`, `cancelled`, `failed` |
| started_at | UTC ISO 8601 timestamp, before config/discovery/planning |
| finished_at | UTC ISO 8601 timestamp, null while nonterminal |
| duration_seconds | nonnegative monotonic elapsed seconds, frozen at termination |
| planned | accounting object, null if planning did not finish |
| completed | accounting object, actual successful files and bytes |
| dispositions | object with `completed`, `not_started`, `active`, `interrupted`, `failed` accounting objects |
| file_progress_percent | number 0–100, null before plan exists |
| byte_progress_percent | number 0–100, null before plan exists |
| progress_percent | same as byte_progress_percent |
| eta_seconds | null; no ETA is estimated |
| error | null or diagnostic string truncated to 512 characters |

Every accounting object is `{files: INTEGER, bytes: INTEGER, routes: ARRAY}`.
Each route is `{source: STRING, destination: STRING, files: INTEGER, bytes: INTEGER}`.
Bytes are integer logical file sizes, not allocated blocks or physical I/O.
Routes use authoritative full branch paths, ordered lexicographically by
(source, destination). No logical inventory names are invented. Zero-count
routes are omitted; consumers can join routes on their two paths. Individual
filenames and exception traces are absent. Result size scales with branch pairs,
not file count. Consumers must ignore future additive fields and reject unknown
schema versions.

`planned` records the original immutable plan. Each move has exactly one
execution disposition. Disposition bytes always use its planned size, so their
sum partitions the plan without implying that pending files still exist or are
safe to retry. `not_started` means never attempted; `active` means attempted and
not yet resolved; `interrupted` means a started attempt raised MoveCancelled;
`failed` means a started attempt raised another exception. Missing, changing,
or conflicting files fail according to existing transfer behavior; there is no
new skip/retry policy. Failure after destination commit can leave a destination
copy and source; that attempt is failed, never credited as completed.

Completion is credited only after `execute_move` returns: existing validation,
copy, source-stability/size or SHA verification, destination rename and directory
fsync, and source unlink all succeeded. Its return value now supplies verified
actual bytes. The executor API still returns None. Test/custom executors returning
None use the planned size; production transfers always return actual bytes.
A file may change between planning and copy; existing transfer policy checks
stability during the copy, not against the plan. Consequently actual completed
bytes can differ from disposition/planned bytes, even on full success.

File progress is successful files / planned files × 100. Byte progress is the
**planned size of successful moves** / planned bytes × 100. This measures the
fraction of the authoritative plan completed while actual completed bytes remain
separately available. If planned bytes are zero, byte progress uses file progress;
an empty plan is 100%. Dry-run never credits transfers (a nonempty dry-run is 0%).
Progress advances only after whole moves succeed; active-file copied bytes are
not included. Percentages can advance in large conservative steps.

## Terminal and error behavior

Exit 0 means successful full live execution or successful planning/dry-run.
Exit 130 means cooperative cancellation; previously completed work is retained
and counted. Exit 1 means configuration/discovery/planning/transfer failure.
Ordinary caught runtime errors in machine modes produce a terminal JSON result
and a diagnostic on stderr, with no prose on stdout. CLI syntax/usage errors use
argparse stderr and exit 2, without a result. Help uses argparse's normal output.
Human mode preserves the existing run exception API and main error exit codes.

The mover has no internal configured-duration deadline. Its existing SIGTERM/
SIGINT interface cooperatively cancels active copies and stops new work; a caller
ending a duration window receives `cancelled`, possibly with partial completed
work. The mover cannot infer whether a signal means routine duration expiry or
outage preemption. The caller may classify the duration stop as normal partial
maintenance completion using its own stop reason; it must preserve mover facts.
Non-cancellation worker failure takes precedence over peer cancellation, so an
actual failure cannot be masked by the order concurrent workers finish.

Normal terminal and cooperative-stop results contain authoritative completed
accounting. An unexpected process/VM crash, SIGKILL, or broken output stream may
leave exact partial mover progress unavailable to the caller. No crash-survival
progress persistence is attempted. Transfer safety remains governed by existing
transactional semantics, not this reporting contract.

## Maintenance integration boundary

`nas-config` should deliberately opt into `--json-events`, consume stdout lines
incrementally, retain progress in memory, collect stderr separately for logs,
wait for process/worker termination, and embed the final `result` in its immutable
maintenance report. EOF without a terminal result is incomplete observation,
not evidence of successful completion. Status can show the latest percentage and
file counters; reports preserve source-to-destination accounting. Maintenance
history, deadlines, power admission, parity, SMART and lifecycle stay outside
this repository. PR #83 is intentionally unchanged by this work.
