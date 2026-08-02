"""Client-side redaction.

This is the only layer that can guarantee a secret never leaves the process, so
it runs before anything is queued for export. The platform redacts again on
ingestion; the two are complementary, not redundant -- this one protects
against a platform operator seeing data they should not, the server one protects
against an application that forgot to configure this one.

Kept deliberately small and dependency-free. Sophisticated PII classification
belongs behind :attr:`Redactor.detectors`, where an application can plug in
whatever it already uses.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

__all__ = ["REDACTED", "Redactor"]

REDACTED = "[redacted]"

_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "authentication",
    "api_key",
    "apikey",
    "api-key",
    "access_key",
    "private_key",
    "client_secret",
    "credential",
    "cookie",
    "bearer",
    "ssn",
    "credit_card",
    "card_number",
    "cvv",
)

#: Platform attributes that DO carry user content and must be redacted.
#:
#: The platform's own namespaces are handled by an inverted rule: anything under
#: ``aiobs.`` or ``gen_ai.`` is safe *unless* it appears here. Enumerating the
#: safe names instead was tried and was wrong -- ``aiobs.usage.input_tokens``
#: and ``aiobs.latency.time_to_first_token_ms`` both contain "token" and were
#: silently destroyed, zeroing every token count and every latency chart. The
#: sensitive set is small, closed and reviewable; the safe set is open-ended.
_PLATFORM_SENSITIVE: frozenset[str] = frozenset(
    {
        "aiobs.input.value",
        "aiobs.output.value",
        "aiobs.prompt.variables",
        "aiobs.retrieval.query",
        "aiobs.retrieval.rewritten_query",
        "aiobs.retrieval.documents",
        "aiobs.agent.goal",
        "aiobs.agent.decision_summary",
        "aiobs.agent.tool.arguments",
        "exception.message",
        "exception.stacktrace",
        "db.query.text",
        "url.full",
    }
)

#: Namespaces the platform owns, whose sensitivity is declared rather than guessed.
_PLATFORM_PREFIXES: tuple[str, ...] = ("aiobs.", "gen_ai.")

_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)


class Redactor:
    """Removes secrets from attributes and payloads before export."""

    __slots__ = ("_allowed", "_extra_keys", "_max_chars", "detectors")

    def __init__(
        self,
        *,
        redact_keys: Iterable[str] = (),
        allowed_keys: Iterable[str] = (),
        max_chars: int = 8_192,
        detectors: Iterable[tuple[str, Callable[[str], bool]]] = (),
    ) -> None:
        self._extra_keys = tuple(key.lower() for key in redact_keys)
        self._allowed = frozenset(allowed_keys)
        self._max_chars = max_chars
        self.detectors = tuple(detectors)

    def is_sensitive(self, key: str) -> bool:
        """Whether ``key``'s value must be removed.

        Platform-owned attributes are judged by the declared sensitive set;
        only unknown, application-supplied keys fall through to the substring
        heuristics -- which is the only place a heuristic belongs.
        """
        lowered = key.lower()
        if key.startswith(_PLATFORM_PREFIXES):
            return key in _PLATFORM_SENSITIVE or lowered in self._extra_keys
        return any(part in lowered for part in (*_KEY_PARTS, *self._extra_keys))

    def value(self, text: str) -> str:
        """Scrub high-confidence secrets from a string."""
        result = text
        for _, pattern in _VALUE_PATTERNS:
            if pattern.search(result):
                result = pattern.sub(REDACTED, result)
        for _, detector in self.detectors:
            try:
                if detector(result):
                    return REDACTED
            except Exception:
                continue
        return result

    def payload(self, text: str | None) -> str | None:
        """Scrub and truncate a prompt or completion."""
        if text is None:
            return None
        cleaned = self.value(text)
        if len(cleaned) > self._max_chars:
            return cleaned[: self._max_chars] + f"…[{len(cleaned)} chars truncated]"
        return cleaned

    def attributes(self, attributes: Mapping[str, Any]) -> dict[str, Any]:
        """Redact an attribute map, returning a new dictionary."""
        result: dict[str, Any] = {}
        for key, item in attributes.items():
            if (
                self._allowed
                and key not in self._allowed
                and not key.startswith(_PLATFORM_PREFIXES)
            ):
                continue
            if self.is_sensitive(key):
                result[key] = REDACTED
                continue
            result[key] = self._walk(item, 0)
        return result

    def _walk(self, value: Any, depth: int) -> Any:
        if depth > 6:
            return REDACTED
        if isinstance(value, str):
            cleaned = self.value(value)
            return cleaned[: self._max_chars] + "…" if len(cleaned) > self._max_chars else cleaned
        if isinstance(value, Mapping):
            return {
                key: REDACTED if self.is_sensitive(str(key)) else self._walk(item, depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._walk(item, depth + 1) for item in value]
        return value
