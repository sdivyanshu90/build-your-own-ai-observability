"""Structured logging.

Every log record is a JSON object with a stable set of keys. That is not
aesthetic preference: the platform's own runbooks query these logs by field,
and a human-formatted string would make ``organization_id`` ungreppable.

Three processors do the interesting work:

``_merge_request_context``
    Copies the ambient request id, principal and tenant onto every record, so a
    log line emitted six frames deep is still attributable.

``_redact_secrets``
    Strips values whose *key* looks sensitive, at the last possible moment. A
    developer who writes ``log.info("auth", password=value)`` gets
    ``password='[redacted]'``, not an incident.

``_bound_message_size``
    Truncates oversized values. A 4 MB prompt in a log line will take down a log
    pipeline just as effectively as a bug will.

Console rendering is used in development only; production always emits JSON.
"""

from __future__ import annotations

import logging
import logging.config
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

from .config import LogFormat, Settings
from .context import get_context

__all__ = ["configure_logging", "get_logger"]

#: Substrings that mark a log field as sensitive. Matching is on the key, not
#: the value, because value-based detection has false negatives that matter.
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "cookie",
    "session_id",
    "jwt",
    "signature",
)

_REDACTED = "[redacted]"
_MAX_VALUE_CHARS = 4_096


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_secrets(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if _is_sensitive(key):
            event_dict[key] = _REDACTED
        elif isinstance(event_dict[key], dict):
            event_dict[key] = _redact_mapping(event_dict[key])
    return event_dict


def _redact_mapping(mapping: MutableMapping[str, Any], depth: int = 0) -> dict[str, Any]:
    if depth > 4:
        return {"_truncated": True}
    result: dict[str, Any] = {}
    for key, value in mapping.items():
        if _is_sensitive(str(key)):
            result[key] = _REDACTED
        elif isinstance(value, dict):
            result[key] = _redact_mapping(value, depth + 1)
        else:
            result[key] = value
    return result


def _bound_message_size(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    for key, value in event_dict.items():
        if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
            event_dict[key] = value[:_MAX_VALUE_CHARS] + f"...[{len(value)} chars truncated]"
    return event_dict


def _merge_request_context(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    context = get_context()
    if context is not None:
        for key, value in context.as_log_fields().items():
            event_dict.setdefault(key, value)
    return event_dict


def _add_service_identity(service: str, version: str, commit: str) -> Processor:
    def processor(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("version", version)
        event_dict.setdefault("commit", commit)
        return event_dict

    return processor


def configure_logging(settings: Settings) -> None:
    """Install the logging configuration for this process.

    Idempotent: calling it twice (as the API does when reloading in
    development) reconfigures rather than stacking handlers.
    """
    level = getattr(logging, settings.log_level)

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _merge_request_context,
        _add_service_identity(settings.service_name, settings.version, settings.git_commit),
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_secrets,
        _bound_message_size,
    ]

    renderer: Processor
    if settings.log_format is LogFormat.JSON:
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party loggers that are chatty or that duplicate our own records.
    for name, noisy_level in (
        ("uvicorn.access", logging.WARNING),  # replaced by our access middleware
        ("uvicorn.error", logging.INFO),
        ("aiokafka", logging.WARNING),
        ("botocore", logging.WARNING),
        ("boto3", logging.WARNING),
        ("urllib3", logging.WARNING),
        ("asyncio", logging.WARNING),
        ("clickhouse_connect", logging.WARNING),
        ("sqlalchemy.engine", logging.INFO if settings.database.echo else logging.WARNING),
    ):
        logging.getLogger(name).setLevel(noisy_level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name``.

    Prefer module-level ``log = get_logger(__name__)`` so that records carry the
    emitting module, which is how the runbooks locate code from a log line.
    """
    return structlog.stdlib.get_logger(name)
