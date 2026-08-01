"""Sensitive-data redaction.

Redaction runs in two places, and both are necessary:

**In the SDK, before anything leaves the process.** This is the only layer that
can guarantee a secret never crosses the network. It is also the only layer the
platform operator does not control, so it cannot be the only layer.

**In the ingestion pipeline, before anything is stored.** This is the layer the
operator controls and can change retroactively for future data. It is the
backstop for applications that forgot to configure the SDK.

The policy is deliberately *allowlist-leaning for keys and blocklist-leaning
for values*: key names are a small, knowable set, so an unknown key that looks
sensitive is redacted; values are unbounded, so value-based detection is used
only for high-confidence patterns (credit cards with a Luhn check, private key
headers) where a false positive is cheaper than a leak.

What this module does **not** do is claim to be a PII detector. It provides a
hook (:class:`RedactionPolicy.detectors`) so an operator can plug in a real
classifier, and it is honest in the documentation that regexes catch the
obvious cases and miss the rest.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aiobs_schemas import semconv

__all__ = [
    "REDACTED_MARKER",
    "RedactionPolicy",
    "RedactionResult",
    "Redactor",
    "default_sensitive_key_patterns",
]

REDACTED_MARKER = "[redacted]"
_TRUNCATION_SUFFIX = "…[truncated]"

#: Substrings that mark an *unregistered* key as sensitive regardless of value.
#:
#: These are heuristics for attributes the platform has never seen. They are
#: deliberately not applied to registered semantic-convention attributes,
#: whose sensitivity is declared rather than guessed -- otherwise
#: ``aiobs.usage.input_tokens`` would be destroyed for containing "token", and
#: the platform would silently lose every token count it exists to report.
#:
#: Patterns short enough to collide with ordinary words ("auth" matching
#: "author", "pin" matching "spinner", "session" matching "session_count") are
#: excluded; the longer specific forms below cover the real risks.
_DEFAULT_KEY_PARTS: tuple[str, ...] = (
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
    "signature",
    "ssn",
    "social_security",
    "credit_card",
    "card_number",
    "cvv",
    "bearer",
)

#: High-confidence value patterns. Kept small on purpose: every additional
#: pattern is a chance to mangle legitimate prompt content, which makes the
#: product worse at its actual job.
_VALUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}=*", re.IGNORECASE)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    (
        "credit_card",
        re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    ),
)


class RedactionMode(str, Enum):
    """How aggressively to redact."""

    #: Redact nothing. Only ever appropriate for a local development project
    #: with synthetic data.
    OFF = "off"
    #: Redact by key name and high-confidence value patterns. The default.
    STANDARD = "standard"
    #: Additionally drop every payload body, keeping only references and
    #: metadata. Appropriate for regulated production environments.
    STRICT = "strict"


def default_sensitive_key_patterns() -> tuple[str, ...]:
    return _DEFAULT_KEY_PARTS


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Per-project redaction configuration."""

    mode: RedactionMode = RedactionMode.STANDARD
    #: When non-empty, ONLY these attribute keys are kept. An allowlist is the
    #: strongest available control and the right choice for regulated data.
    allowlist: frozenset[str] = field(default_factory=frozenset)
    #: Keys always removed, in addition to the built-in patterns.
    blocklist: frozenset[str] = field(default_factory=frozenset)
    #: Substrings that mark a key sensitive.
    key_patterns: tuple[str, ...] = _DEFAULT_KEY_PARTS
    #: Enable/disable individual value detectors by name.
    value_detectors: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"private_key", "aws_access_key", "bearer_token", "jwt", "credit_card"}
        )
    )
    #: Inline payloads longer than this are truncated (and flagged).
    max_value_length: int = 16_384
    #: Optional pluggable classifiers: ``(name, callable(value) -> bool)``.
    detectors: tuple[tuple[str, Callable[[str], bool]], ...] = ()

    @classmethod
    def strict(cls) -> RedactionPolicy:
        return cls(mode=RedactionMode.STRICT)

    @classmethod
    def disabled(cls) -> RedactionPolicy:
        return cls(mode=RedactionMode.OFF)


@dataclass(slots=True)
class RedactionResult:
    """Redacted data plus an auditable record of what was removed."""

    value: Any
    #: Attribute keys whose values were replaced.
    redacted_keys: list[str] = field(default_factory=list)
    #: Keys that were truncated rather than removed.
    truncated_keys: list[str] = field(default_factory=list)
    #: Detector names that fired, for metrics. Never contains the matched text.
    detectors_fired: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.redacted_keys or self.truncated_keys)


def _luhn_valid(digits: str) -> bool:
    """Luhn checksum, used to avoid redacting every long number as a card."""
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = ord(char) - 48
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


class Redactor:
    """Applies a :class:`RedactionPolicy` to attributes and payloads."""

    __slots__ = ("_key_patterns", "_policy")

    def __init__(self, policy: RedactionPolicy | None = None) -> None:
        self._policy = policy or RedactionPolicy()
        self._key_patterns = tuple(part.lower() for part in self._policy.key_patterns)

    @property
    def policy(self) -> RedactionPolicy:
        return self._policy

    def is_sensitive_key(self, key: str) -> bool:
        """Whether ``key`` should have its value removed.

        Registered semantic-convention attributes are judged by their *declared*
        sensitivity. Only unknown, application-supplied keys fall through to the
        substring heuristics -- which is the only place a heuristic belongs,
        because for registered attributes we already know the answer.
        """
        specification = semconv.lookup(key)
        if specification is not None:
            return specification.sensitive or key.lower() in self._policy.blocklist
        lowered = key.lower()
        if lowered in self._policy.blocklist:
            return True
        return any(part in lowered for part in self._key_patterns)

    def redact_value(self, value: str) -> tuple[str, list[str]]:
        """Apply value detectors to a string, returning the result and hits."""
        if self._policy.mode is RedactionMode.OFF:
            return value, []
        fired: list[str] = []
        result = value
        for name, pattern in _VALUE_PATTERNS:
            if name not in self._policy.value_detectors:
                continue
            if name == "credit_card":
                result, hit = _redact_credit_cards(result)
                if hit:
                    fired.append(name)
                continue
            if pattern.search(result):
                result = pattern.sub(REDACTED_MARKER, result)
                fired.append(name)
        for name, detector in self._policy.detectors:
            try:
                if detector(result):
                    fired.append(name)
                    result = REDACTED_MARKER
                    break
            except Exception:
                continue
        return result, fired

    def redact_attributes(self, attributes: Mapping[str, Any]) -> RedactionResult:
        """Redact an attribute map according to the policy."""
        if self._policy.mode is RedactionMode.OFF:
            return RedactionResult(value=dict(attributes))

        cleaned: dict[str, Any] = {}
        result = RedactionResult(value=cleaned)

        for key, value in attributes.items():
            if self._policy.allowlist and key not in self._policy.allowlist:
                # Allowlist mode: unknown keys are dropped entirely rather than
                # marked, so their very presence is not disclosed.
                result.redacted_keys.append(key)
                continue
            if self.is_sensitive_key(key):
                cleaned[key] = REDACTED_MARKER
                result.redacted_keys.append(key)
                continue
            cleaned[key] = self._redact_any(key, value, result)

        result.redacted_keys.sort()
        result.truncated_keys.sort()
        return result

    def _redact_any(self, key: str, value: Any, result: RedactionResult, depth: int = 0) -> Any:
        if depth > 8:
            return REDACTED_MARKER
        if isinstance(value, str):
            redacted, fired = self.redact_value(value)
            if fired:
                result.redacted_keys.append(key)
                result.detectors_fired.extend(fired)
            if len(redacted) > self._policy.max_value_length:
                result.truncated_keys.append(key)
                return redacted[: self._policy.max_value_length] + _TRUNCATION_SUFFIX
            return redacted
        if isinstance(value, Mapping):
            return {
                sub_key: (
                    REDACTED_MARKER
                    if self.is_sensitive_key(str(sub_key))
                    else self._redact_any(f"{key}.{sub_key}", sub_value, result, depth + 1)
                )
                for sub_key, sub_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._redact_any(key, item, result, depth + 1) for item in value]
        return value

    def redact_payload(self, payload: str | None) -> tuple[str | None, bool]:
        """Redact a prompt/completion/tool payload.

        Returns ``(payload, was_removed)``. In STRICT mode the payload is
        dropped entirely -- the span keeps its object-storage reference and its
        size, so an operator with the right role can still retrieve it, while the
        stored analytics row carries nothing.
        """
        if payload is None:
            return None, False
        if self._policy.mode is RedactionMode.OFF:
            return payload, False
        if self._policy.mode is RedactionMode.STRICT:
            return None, True
        redacted, fired = self.redact_value(payload)
        if len(redacted) > self._policy.max_value_length:
            redacted = redacted[: self._policy.max_value_length] + _TRUNCATION_SUFFIX
        return redacted, bool(fired)

    def redact_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        """Redact HTTP headers. Authorization and Cookie always go."""
        return {
            key: (REDACTED_MARKER if self.is_sensitive_key(key) else value)
            for key, value in headers.items()
        }

    def redact_url(self, url: str) -> str:
        """Strip credentials and sensitive query parameters from a URL."""
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        try:
            parts = urlsplit(url)
        except ValueError:
            return REDACTED_MARKER
        netloc = parts.netloc
        if "@" in netloc:
            # userinfo in a URL is a credential; never store it.
            netloc = REDACTED_MARKER + "@" + netloc.rsplit("@", 1)[1]
        query = urlencode(
            [
                (key, REDACTED_MARKER if self.is_sensitive_key(key) else value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _redact_credit_cards(value: str) -> tuple[str, bool]:
    """Redact digit runs that pass a Luhn check.

    The Luhn gate is what keeps this usable: without it, every order number,
    trace id fragment and long integer in a prompt would be destroyed.
    """
    pattern = _VALUE_PATTERNS[-1][1]
    fired = False

    def replace(match: re.Match[str]) -> str:
        nonlocal fired
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            fired = True
            return REDACTED_MARKER
        return match.group(0)

    return pattern.sub(replace, value), fired


def merge_policies(base: RedactionPolicy, overrides: Mapping[str, Any] | None) -> RedactionPolicy:
    """Apply a project's stored settings on top of the platform default.

    Only *tightening* is permitted from project settings: a project may add to
    the blocklist or switch to strict mode, but cannot switch redaction off if
    the platform default is standard. Otherwise a tenant could opt out of the
    operator's compliance posture.
    """
    if not overrides:
        return base
    mode = base.mode
    requested = overrides.get("mode")
    if requested in {mode.value for mode in RedactionMode}:
        candidate = RedactionMode(requested)
        ranking = {RedactionMode.OFF: 0, RedactionMode.STANDARD: 1, RedactionMode.STRICT: 2}
        if ranking[candidate] >= ranking[base.mode]:
            mode = candidate
    return RedactionPolicy(
        mode=mode,
        allowlist=frozenset(overrides.get("allowlist", base.allowlist)),
        blocklist=frozenset(base.blocklist) | frozenset(overrides.get("blocklist", ())),
        key_patterns=tuple(base.key_patterns) + tuple(overrides.get("key_patterns", ())),
        value_detectors=frozenset(overrides.get("value_detectors", base.value_detectors)),
        max_value_length=min(
            int(overrides.get("max_value_length", base.max_value_length)),
            base.max_value_length,
        ),
        detectors=base.detectors,
    )


def redact_sequence(redactor: Redactor, values: Iterable[str]) -> Sequence[str]:
    """Redact each string in a sequence, dropping detector bookkeeping."""
    return [redactor.redact_value(value)[0] for value in values]
