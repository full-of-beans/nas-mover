"""Versioned in-memory accounting; no filesystem or history ownership."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from collections.abc import Callable

from .models import PlannedMove
from .transfer import MoveCancelled


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def aggregate(items: list[tuple[PlannedMove, int]]) -> dict:
    routes: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for move, size in items:
        counts = routes[str(move.source_branch.path), str(move.destination_branch.path)]
        counts[0] += 1
        counts[1] += size
    return {
        "files": len(items), "bytes": sum(size for _, size in items),
        "routes": [dict(source=source, destination=destination, files=counts[0], bytes=counts[1])
                   for (source, destination), counts in sorted(routes.items())],
    }


class ExecutionReport:
    def __init__(self, *, live: bool, emit: Callable[[dict], None] | None = None) -> None:
        self.mode = "live" if live else "dry_run"
        self.started_at = timestamp()
        self.started = time.monotonic()
        self.finished_at: str | None = None
        self.duration: float | None = None
        self.status = "planning"
        self.error: str | None = None
        self.moves: list[PlannedMove] | None = None
        self.states: dict[int, str] = {}
        self.actual: dict[int, int] = {}
        self.lock = threading.Lock()
        self.emit = emit
        self.last_event = float("-inf")

    def plan(self, moves: list[PlannedMove]) -> None:
        self.moves = moves
        self.states = {id(move): "not_started" for move in moves}
        self.status = "running" if self.mode == "live" else "planned"
        self.event("planned")

    def snapshot(self) -> dict:
        planned = None if self.moves is None else aggregate([(m, m.size) for m in self.moves])
        completed = aggregate([(m, self.actual[id(m)]) for m in self.moves or []
                               if self.states[id(m)] == "completed"])
        dispositions = {state: aggregate([(m, m.size) for m in self.moves or []
                                           if self.states[id(m)] == state])
                        for state in ("completed", "not_started", "active", "interrupted", "failed")}
        credited = dispositions["completed"]["bytes"]
        files = None if planned is None else (100.0 * completed["files"] / planned["files"] if planned["files"] else 100.0)
        bytes_percent = None if planned is None else (100.0 * credited / planned["bytes"] if planned["bytes"] else files)
        return dict(schema_version=1, mode=self.mode, status=self.status,
                    started_at=self.started_at, finished_at=self.finished_at,
                    duration_seconds=self.duration if self.duration is not None else time.monotonic() - self.started,
                    planned=planned, completed=completed, dispositions=dispositions,
                    file_progress_percent=files, byte_progress_percent=bytes_percent,
                    progress_percent=bytes_percent, eta_seconds=None, error=self.error)

    def event(self, kind: str) -> None:
        if self.emit is not None:
            self.emit(dict(schema_version=1, event=kind, result=self.snapshot()))

    def wrap(self, executor: Callable) -> Callable:
        def execute(move: PlannedMove, **kwargs: object) -> None:
            with self.lock:
                self.states[id(move)] = "active"
            try:
                size = executor(move, **kwargs)
            except BaseException as exc:
                with self.lock:
                    self.states[id(move)] = "interrupted" if isinstance(exc, MoveCancelled) else "failed"
                raise
            with self.lock:
                self.states[id(move)] = "completed"
                self.actual[id(move)] = move.size if size is None else size
                now = time.monotonic()
                if now - self.last_event >= 1.0:
                    self.last_event = now
                    self.event("progress")
        return execute

    def finish(self, status: str, error: str | None = None) -> dict:
        self.status = status
        self.error = None if error is None else error[:512]
        self.finished_at = timestamp()
        self.duration = time.monotonic() - self.started
        self.event("result")
        return self.snapshot()


def render(result: dict) -> str:
    lines = [f"{'LIVE' if result['mode'] == 'live' else 'DRY RUN'}: {result['status'].upper()}"]
    if result["planned"] is not None:
        actual = {(r["source"], r["destination"]): r for r in result["completed"]["routes"]}
        lines.append("SOURCE -> DESTINATION | PLANNED files / bytes | COMPLETED files / bytes")
        for route in result["planned"]["routes"]:
            done = actual.get((route["source"], route["destination"]), {"files": 0, "bytes": 0})
            lines.append(f"{route['source']} -> {route['destination']} | {route['files']} / {route['bytes']} | {done['files']} / {done['bytes']}")
        lines.append(f"TOTAL: planned {result['planned']['files']} files / {result['planned']['bytes']} bytes; completed {result['completed']['files']} files / {result['completed']['bytes']} bytes")
    if result["error"]:
        lines.append(f"ERROR: {result['error']}")
    return "\n".join(lines)
