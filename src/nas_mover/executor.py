from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from .models import PlannedMove
from .transfer import MoveCancelled, Verification, execute_move

MoveExecutor = Callable[..., None]


def _run_serial_queue(
    moves: list[PlannedMove],
    *,
    verify: Verification,
    cancel_event: threading.Event,
    move_executor: MoveExecutor,
) -> None:
    for move in moves:
        if cancel_event.is_set():
            raise MoveCancelled("Mover cancellation requested")
        move_executor(move, verify=verify, cancel_requested=cancel_event.is_set)


def execute_moves(
    moves: Iterable[PlannedMove],
    *,
    verify: Verification = "size",
    cancel_event: threading.Event | None = None,
    move_executor: MoveExecutor = execute_move,
) -> None:
    """Execute one authoritative plan with serial queues per HDD destination.

    SSD-destination moves remain sequential. HDD moves sharing a destination branch are
    serialized; different HDD destination branches may execute concurrently. Any worker
    failure requests cooperative cancellation for all remaining workers. This function
    waits for every worker before returning or raising, so callers can safely release the
    global process lock afterward.
    """

    cancel = cancel_event or threading.Event()
    sequential: list[PlannedMove] = []
    hdd_queues: dict[str, list[PlannedMove]] = defaultdict(list)

    for move in moves:
        if move.destination_branch.rotational:
            hdd_queues[str(move.destination_branch.path)].append(move)
        else:
            sequential.append(move)

    for move in sequential:
        if cancel.is_set():
            raise MoveCancelled("Mover cancellation requested")
        move_executor(move, verify=verify, cancel_requested=cancel.is_set)

    if not hdd_queues:
        return

    futures: list[Future[None]] = []
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=len(hdd_queues), thread_name_prefix="nas-mover") as pool:
        for queue in hdd_queues.values():
            futures.append(
                pool.submit(
                    _run_serial_queue,
                    queue,
                    verify=verify,
                    cancel_event=cancel,
                    move_executor=move_executor,
                )
            )
        for future in as_completed(futures):
            try:
                future.result()
            except BaseException as exc:
                errors.append(exc)
                cancel.set()
        # ThreadPoolExecutor context exit joins every worker.

    if errors:
        raise errors[0]
