"""Span buffering and export.

A background thread owns the buffer and the HTTP client. That choice is
deliberate: a thread works identically under sync code, asyncio, gevent and
multiple event loops, whereas an asyncio task would force every caller to have
a running loop and would stop flushing the moment the loop is blocked.

Failure behaviour, in order of importance:

1. **The application never blocks on telemetry.** ``submit`` puts a span in a
   bounded queue and returns; it never performs I/O.
2. **A full queue drops the oldest spans, not the newest.** During an incident
   the recent spans are the ones being looked at.
3. **Retries are bounded and jittered.** A platform outage must not turn into a
   retry storm from every instrumented application.
4. **Shutdown flushes, with a deadline.** A process exiting should deliver what
   it has, but must not hang because the collector is down.
"""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import queue
import random
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from .config import Config

__all__ = ["BatchExporter", "ExportResult", "HttpTransport", "Transport"]

log = logging.getLogger("aiobs")

SDK_NAME = "aiobs-python"
SDK_VERSION = "0.1.0"


class ExportResult:
    """Outcome of one export attempt."""

    __slots__ = ("accepted", "duplicates", "error", "rejected", "retryable")

    def __init__(
        self,
        *,
        accepted: int = 0,
        rejected: int = 0,
        duplicates: int = 0,
        retryable: bool = False,
        error: str | None = None,
    ) -> None:
        self.accepted = accepted
        self.rejected = rejected
        self.duplicates = duplicates
        self.retryable = retryable
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None

    def __repr__(self) -> str:
        return (
            f"ExportResult(accepted={self.accepted}, rejected={self.rejected}, "
            f"duplicates={self.duplicates}, error={self.error!r})"
        )


class Transport:
    """Sends a batch. Subclassed by tests to capture spans without a server."""

    def send(self, payload: dict[str, Any]) -> ExportResult:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        return None


class HttpTransport(Transport):
    """Posts batches to the platform's native ingest endpoint."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.Client(
                timeout=self._config.timeout_seconds,
                headers={
                    "X-API-Key": self._config.api_key or "",
                    "User-Agent": f"{SDK_NAME}/{SDK_VERSION}",
                    "Content-Type": "application/json",
                },
                # Connection reuse matters: at 200-span batches every few
                # seconds, a fresh TLS handshake per export is pure waste.
                limits=__import__("httpx").Limits(max_keepalive_connections=4, max_connections=8),
            )
        return self._client

    def send(self, payload: dict[str, Any]) -> ExportResult:
        import httpx

        client = self._ensure_client()
        body = json.dumps(payload, default=str).encode("utf-8")
        headers: dict[str, str] = {}
        if self._config.compress and len(body) > 1_024:
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"

        try:
            response = client.post(self._config.ingest_url, content=body, headers=headers)
        except httpx.TimeoutException as exc:
            return ExportResult(retryable=True, error=f"timeout: {exc}")
        except httpx.HTTPError as exc:
            return ExportResult(retryable=True, error=f"transport: {exc}")

        if response.status_code in (200, 202):
            try:
                data = response.json()
            except ValueError:
                return ExportResult(accepted=len(payload.get("spans", [])))
            return ExportResult(
                accepted=int(data.get("accepted", 0)),
                rejected=int(data.get("rejected", 0)),
                duplicates=int(data.get("duplicates", 0)),
            )

        # 4xx other than 429 means the payload is wrong; retrying cannot fix it
        # and would waste the application's resources forever.
        retryable = response.status_code == 429 or response.status_code >= 500
        detail = response.text[:400]
        return ExportResult(retryable=retryable, error=f"http {response.status_code}: {detail}")

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


class BatchExporter:
    """Buffers spans and exports them from a background thread."""

    def __init__(
        self,
        config: Config,
        transport: Transport | None = None,
        *,
        resource: dict[str, Any] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or HttpTransport(config)
        self._resource = resource or {}
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=config.max_queue_size)
        self._lock = threading.Lock()
        self._flush_now = threading.Event()
        self._stopping = threading.Event()
        self._flushed = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

        # Counters, exposed for tests and for the SDK's own health reporting.
        self.submitted = 0
        self.dropped = 0
        self.exported = 0
        self.failed = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run,
            name="aiobs-exporter",
            # Daemon so a forgotten shutdown cannot hang the process; the
            # atexit hook below is what actually flushes on a clean exit.
            daemon=True,
        )
        self._thread.start()
        atexit.register(self.shutdown)

    def submit(self, span: dict[str, Any]) -> bool:
        """Queue a span. Returns ``False`` if it was dropped."""
        if self._stopping.is_set():
            return False
        self.submitted += 1
        try:
            self._queue.put_nowait(span)
        except queue.Full:
            # Drop the oldest to make room. A best-effort dequeue: if another
            # thread beat us to it the put below simply fails again and we
            # count a drop, which is the correct outcome either way.
            try:
                self._queue.get_nowait()
                self.dropped += 1
                self._queue.put_nowait(span)
            except (queue.Empty, queue.Full):
                self.dropped += 1
                return False
        if self._queue.qsize() >= self._config.max_batch_size:
            self._flush_now.set()
        return True

    def flush(self, timeout: float | None = None) -> bool:
        """Block until the buffer has been exported at least once.

        Used by tests and by short-lived scripts that would otherwise exit
        before the background thread ran.
        """
        if not self._started:
            self._drain_once()
            return True
        self._flushed.clear()
        self._flush_now.set()
        return self._flushed.wait(timeout or self._config.shutdown_timeout_seconds)

    def shutdown(self) -> None:
        """Stop the exporter, flushing what is buffered within the deadline."""
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._flush_now.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._config.shutdown_timeout_seconds)
        # Whatever is left is exported synchronously; a process that is exiting
        # has nothing better to do, and losing the last batch of a short script
        # is the single most confusing SDK behaviour there is.
        self._drain_once()
        self._transport.close()

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._flush_now.wait(timeout=self._config.flush_interval_seconds)
            self._flush_now.clear()
            try:
                self._drain_once()
            except Exception as exc:
                self._record_error(exc)
            finally:
                self._flushed.set()
        try:
            self._drain_once()
        except Exception as exc:
            self._record_error(exc)
        finally:
            self._flushed.set()

    def _drain_once(self) -> None:
        while True:
            batch = self._take_batch()
            if not batch:
                return
            self._export_with_retry(batch)

    def _take_batch(self) -> list[dict[str, Any]]:
        batch: list[dict[str, Any]] = []
        while len(batch) < self._config.max_batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _export_with_retry(self, spans: Sequence[dict[str, Any]]) -> None:
        if not self._config.can_export:
            # No credentials: the spans were still built (tests inspect them)
            # but there is nowhere to send them.
            return
        payload = {
            "resource": self._resource,
            "spans": list(spans),
            "sampling_rate": self._config.sample_rate,
        }
        attempt = 0
        while attempt <= self._config.max_retries:
            result = self._transport.send(payload)
            if result.ok:
                self.exported += result.accepted
                if result.rejected and self._config.debug:
                    log.warning("aiobs: %d spans rejected by the platform", result.rejected)
                return
            if not result.retryable:
                self.failed += len(spans)
                self.last_error = result.error
                log.warning("aiobs: dropping %d spans: %s", len(spans), result.error)
                return
            attempt += 1
            if attempt > self._config.max_retries:
                break
            delay = min(
                self._config.retry_base_delay_seconds * (2 ** (attempt - 1)),
                self._config.retry_max_delay_seconds,
            )
            # Full jitter: every instrumented process retrying in lockstep after
            # a platform blip is how a blip becomes an outage.
            time.sleep(random.uniform(0.0, delay))
        self.failed += len(spans)
        self.last_error = result.error
        log.warning(
            "aiobs: giving up on %d spans after %d attempts: %s",
            len(spans),
            self._config.max_retries,
            result.error,
        )

    def _record_error(self, exc: BaseException) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        handler: Callable[[BaseException], None] | None = self._config.on_error
        if handler is not None:
            try:
                handler(exc)
                return
            except Exception:
                pass
        log.warning("aiobs: exporter error: %s", self.last_error)

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "dropped": self.dropped,
            "exported": self.exported,
            "failed": self.failed,
            "queued": self._queue.qsize(),
            "last_error": self.last_error,
        }


class MemoryTransport(Transport):
    """Captures batches in memory. The SDK's primary test utility."""

    def __init__(self, *, fail_times: int = 0, retryable: bool = True) -> None:
        self.batches: list[dict[str, Any]] = []
        self.spans: list[dict[str, Any]] = []
        self._fail_times = fail_times
        self._retryable = retryable
        self.attempts = 0
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> ExportResult:
        with self._lock:
            self.attempts += 1
            if self._fail_times > 0:
                self._fail_times -= 1
                return ExportResult(retryable=self._retryable, error="simulated transport failure")
            self.batches.append(payload)
            self.spans.extend(payload.get("spans", []))
            return ExportResult(accepted=len(payload.get("spans", [])))

    def clear(self) -> None:
        with self._lock:
            self.batches.clear()
            self.spans.clear()
            self.attempts = 0
