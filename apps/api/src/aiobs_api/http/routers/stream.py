"""Server-sent events for live trace updates.

SSE rather than WebSockets: the data flows one way, SSE reconnects
automatically with ``Last-Event-ID``, and it survives HTTP proxies that mangle
WebSocket upgrades. A bidirectional protocol would be complexity with no
corresponding capability.

The stream polls the analytics store rather than tailing the bus. That is a
deliberate simplification: it keeps the API stateless (any replica can serve any
subscriber), it automatically reflects the *processed* state rather than the
queued one, and the poll is a cheap indexed range scan. The cost is up to one
poll interval of latency, which is immaterial next to the ingestion pipeline's
own end-to-end delay.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query, Request
from sse_starlette.sse import EventSourceResponse

from ...core.logging import get_logger
from ...core.query import PageRequest
from ...domain.rbac import Permission
from ...storage.analytics.rows import AnalyticsScope
from ..deps import PrincipalDep, ServicesDep
from ..schemas import TraceOut

__all__ = ["router"]

log = get_logger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])

_POLL_SECONDS = 2.0
#: Closed after this long so a forgotten browser tab cannot hold a connection
#: open indefinitely. The client's automatic reconnect makes this invisible.
_MAX_STREAM_SECONDS = 3_600
_MAX_BATCH = 50


@router.get(
    "/traces",
    summary="Live trace stream (SSE)",
    description=(
        "Emits a `trace` event for each newly-completed trace, and a "
        "`heartbeat` every few seconds so proxies do not time the connection "
        "out. Reconnect with `Last-Event-ID` to resume."
    ),
)
async def stream_traces(
    request: Request,
    principal: PrincipalDep,
    services: ServicesDep,
    project_id: Annotated[str, Query()],
    environment: Annotated[str | None, Query()] = None,
) -> EventSourceResponse:
    principal.require(Permission.TRACE_READ)
    principal.require_project(project_id)
    scope = AnalyticsScope(
        organization_id=principal.organization_id,
        project_id=project_id,
        environment=environment,
    )
    clock = services.container.clock

    async def publisher() -> AsyncIterator[dict[str, str]]:
        # Start from "now minus one window" so a subscriber sees recent context
        # immediately instead of an empty pane until the next trace arrives.
        cursor_time = clock.now() - timedelta(seconds=30)
        started = clock.now()
        seen: set[str] = set()

        while True:
            if await request.is_disconnected():
                return
            if (clock.now() - started).total_seconds() > _MAX_STREAM_SECONDS:
                yield {"event": "close", "data": json.dumps({"reason": "max_duration"})}
                return

            now = clock.now()
            try:
                page = await services.container.analytics.search_traces(
                    scope,
                    start=cursor_time,
                    end=now,
                    page=PageRequest(limit=_MAX_BATCH),
                )
            except Exception as exc:
                log.warning("stream.poll_failed", error=str(exc))
                await asyncio.sleep(_POLL_SECONDS * 2)
                continue

            emitted = 0
            for trace in reversed(page.items):
                if trace.trace_id in seen:
                    continue
                seen.add(trace.trace_id)
                emitted += 1
                yield {
                    "event": "trace",
                    "id": trace.trace_id,
                    "data": TraceOut.from_row(trace).model_dump_json(),
                }

            # Bound the de-duplication set; traces older than the window cannot
            # reappear, so forgetting them is safe and keeps memory flat.
            if len(seen) > 5_000:
                seen = set(list(seen)[-1_000:])

            if emitted == 0:
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"at": now.isoformat()}),
                }
            # Overlap the window slightly so a trace whose roll-up lands between
            # polls is not skipped.
            cursor_time = now - timedelta(seconds=5)
            await asyncio.sleep(_POLL_SECONDS)

    return EventSourceResponse(publisher(), ping=15)
