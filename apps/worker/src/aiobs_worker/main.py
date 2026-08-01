"""Worker entry point.

Runs the bus consumers and the maintenance job runner in one process, under a
supervisor that restarts a crashed task rather than letting the process
degrade silently.

Shutdown is graceful and ordered: on SIGTERM the consumers stop *polling* but
finish the batch in hand, commit it, and only then exit. Killing mid-batch would
be safe (handlers are idempotent, so the batch is simply re-delivered) but it
would produce duplicate work on every deploy, which is avoidable noise.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from aiobs_api.container import build_container
from aiobs_api.core.config import Settings, get_settings
from aiobs_api.core.logging import configure_logging, get_logger
from aiobs_api.services.bundle import build_services
from aiobs_api.storage.bus.protocol import Topics

from .consumers import Consumer
from .jobs import JobRunner, build_jobs

__all__ = ["main", "run_worker"]

log = get_logger(__name__)

#: Where the worker records that it is still making progress.
#:
#: A worker serves no traffic, so there is nothing to probe over HTTP, and a
#: process-alive check would miss the failure that actually matters: a consumer
#: that is running but has stopped consuming. The heartbeat is written by the
#: supervisor loop, so a wedged task stops refreshing it.
HEARTBEAT_PATH = Path(os.environ.get("AIOBS_WORKER__HEARTBEAT_PATH", "/tmp/aiobs-worker.heartbeat"))

#: How stale the heartbeat may be before the worker is considered wedged.
#: Comfortably longer than the interval so a slow batch is not a restart.
HEARTBEAT_TIMEOUT_SECONDS = 120.0
HEARTBEAT_INTERVAL_SECONDS = 15.0


async def _heartbeat(stopping: asyncio.Event) -> None:
    """Touch the heartbeat file while the worker is healthy."""
    while not stopping.is_set():
        try:
            HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")
        except OSError as exc:
            log.warning("worker.heartbeat_failed", error=str(exc))
        try:
            await asyncio.wait_for(stopping.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


def check_health() -> int:
    """Exit 0 if the worker refreshed its heartbeat recently.

    Used as the container liveness probe. Returns non-zero for a missing or
    stale heartbeat, which is what makes a stuck-but-alive consumer restartable.
    """
    try:
        written = float(HEARTBEAT_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        print(f"no readable heartbeat at {HEARTBEAT_PATH}", file=sys.stderr)
        return 1
    age = time.time() - written
    if age > HEARTBEAT_TIMEOUT_SECONDS:
        print(
            f"heartbeat is {age:.0f}s old (limit {HEARTBEAT_TIMEOUT_SECONDS:.0f}s)", file=sys.stderr
        )
        return 1
    print(f"heartbeat is {age:.0f}s old")
    return 0


async def run_worker(
    settings: Settings,
    *,
    run_consumers: bool = True,
    run_jobs: bool = True,
    shutdown_event: asyncio.Event | None = None,
) -> int:
    """Run the worker until stopped. Returns a process exit code."""
    container = build_container(settings)
    await container.start()
    services = build_services(container)

    stopping = shutdown_event or asyncio.Event()
    consumers: list[Consumer] = []

    if run_consumers:
        consumers = [
            Consumer(
                name="spans",
                topic=Topics.SPANS,
                bus=container.bus,
                handler=services.span_processor.process_batch,
                settings=settings,
            ),
            Consumer(
                name="rollup",
                topic=Topics.TRACE_ROLLUP,
                bus=container.bus,
                handler=services.rollup_processor.process_batch,
                settings=settings,
            ),
        ]

    runner = (
        JobRunner(
            services=services,
            jobs=build_jobs(services=services, settings=settings, clock=container.clock),
            clock=container.clock,
        )
        if run_jobs
        else None
    )

    tasks: list[asyncio.Task[None]] = []
    tasks.append(asyncio.create_task(_heartbeat(stopping)))
    for consumer in consumers:
        tasks.append(asyncio.create_task(_supervise(consumer.run, consumer.name, stopping)))
    if runner is not None:
        tasks.append(asyncio.create_task(_supervise(runner.run, "jobs", stopping)))

    log.info(
        "worker.started",
        consumers=[consumer.name for consumer in consumers],
        jobs=bool(runner),
        bus=settings.bus.driver.value,
        analytics=settings.analytics.driver.value,
    )

    await stopping.wait()
    log.info("worker.draining")

    for consumer in consumers:
        consumer.request_stop()
    if runner is not None:
        runner.request_stop()

    # Give in-flight batches a bounded window to finish and commit.
    done, pending = await asyncio.wait(tasks, timeout=settings.shutdown_grace_seconds)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    await container.stop()
    failures = [task for task in done if task.exception() is not None]
    for task in failures:
        log.error("worker.task_failed", error=str(task.exception()))
    log.info("worker.stopped", failed_tasks=len(failures))
    return 1 if failures else 0


async def _supervise(
    run: object, name: str, stopping: asyncio.Event, *, max_restarts: int = 10
) -> None:
    """Restart a crashed task with backoff instead of losing it silently.

    A consumer that dies takes ingestion with it. Restarting bounded-many times
    turns a transient fault into a blip; exceeding the bound sets the shutdown
    event so the orchestrator restarts the whole process, which is the correct
    escalation for a fault that is not transient.
    """
    restarts = 0
    while not stopping.is_set():
        try:
            await run()  # type: ignore[operator]
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            restarts += 1
            if restarts > max_restarts:
                log.error(
                    "worker.task_restart_exhausted",
                    task=name,
                    restarts=restarts,
                    error=str(exc),
                )
                stopping.set()
                raise
            delay = min(2**restarts, 30)
            log.warning(
                "worker.task_restarting",
                task=name,
                restarts=restarts,
                delay_seconds=delay,
                error=str(exc),
            )
            await asyncio.sleep(delay)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
    for signal_name in ("SIGTERM", "SIGINT"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, event.set)
        except NotImplementedError:  # pragma: no cover - Windows
            signal.signal(signal_number, lambda *_: event.set())


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="aiobs-worker", description="AI Observability Platform worker"
    )
    parser.add_argument(
        "--no-consumers",
        action="store_true",
        help="Run only the maintenance jobs (useful for a dedicated cron replica).",
    )
    parser.add_argument(
        "--no-jobs",
        action="store_true",
        help="Run only the bus consumers (useful when scaling ingestion separately).",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="Check the heartbeat of a running worker and exit. Used as the liveness probe.",
    )
    arguments = parser.parse_args(argv)

    if arguments.health_check:
        return check_health()

    settings = get_settings()
    configure_logging(settings)

    async def runner() -> int:
        event = asyncio.Event()
        _install_signal_handlers(asyncio.get_running_loop(), event)
        return await run_worker(
            settings,
            run_consumers=not arguments.no_consumers,
            run_jobs=not arguments.no_jobs,
            shutdown_event=event,
        )

    return asyncio.run(runner())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
