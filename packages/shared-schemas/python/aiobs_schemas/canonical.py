"""Canonical JSON serialisation and content addressing.

Reproducibility of prompt, model-configuration and dataset versions rests on one
property: *the same logical content must always produce the same bytes, in every
language, on every machine, forever.* Only then is a content hash a stable
identity.

This module implements a canonicalisation scheme compatible with
:rfc:`8785` (JSON Canonicalization Scheme, JCS):

* objects are emitted with keys sorted by UTF-16 code unit, as JavaScript's
  ``Array.prototype.sort`` does;
* no insignificant whitespace is emitted;
* strings are Unicode-normalised to NFC and escaped using the shortest legal
  form (``\\b \\t \\n \\f \\r \\" \\\\`` plus ``\\u00XX`` for other control
  characters), never ``\\uXXXX`` for printable characters;
* numbers are formatted with the ECMAScript ``Number::toString`` algorithm so
  that Python and TypeScript agree byte-for-byte;
* ``NaN``/``Infinity`` are rejected rather than silently coerced;
* the result is encoded as UTF-8.

The number formatting is the subtle part and is covered by a cross-language
fixture (``packages/shared-schemas/json/number-canonicalization.json``) that is
generated from Node's ``String(x)`` and asserted by both the Python and the
TypeScript test suites.

See ``docs/concepts/content-addressed-storage.md``.
"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from decimal import Decimal
from typing import Any, Final

__all__ = [
    "HASH_ALGORITHM",
    "CanonicalizationError",
    "canonical_json",
    "canonical_json_str",
    "content_hash",
    "format_number",
    "short_hash",
    "verify_hash",
]

HASH_ALGORITHM: Final = "sha256"
_HASH_PREFIX: Final = f"{HASH_ALGORITHM}:"

#: Guard against pathological or accidentally recursive structures.
_MAX_DEPTH: Final = 64


class CanonicalizationError(ValueError):
    """Raised when a value cannot be canonicalised deterministically."""


_ESCAPES: Final[dict[int, str]] = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def _escape_string(value: str) -> str:
    """Escape ``value`` per RFC 8785 section 3.2.2.2."""
    out: list[str] = ['"']
    for char in unicodedata.normalize("NFC", value):
        code = ord(char)
        escape = _ESCAPES.get(code)
        if escape is not None:
            out.append(escape)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _shortest_digits(value: float) -> tuple[str, int, int]:
    """Decompose ``value`` into ``(digits, point, sign)``.

    ``digits`` is the shortest decimal digit string that round-trips to
    ``value``, with no leading or trailing zeros, and ``point`` is placed so
    that ``abs(value) == 0.<digits> * 10 ** point``. This is steps 5-6 of the
    ECMAScript ``Number::toString`` algorithm; ``repr`` supplies the shortest
    round-tripping digits, exactly as V8's Grisu/Ryu implementation does.
    """
    sign = -1 if math.copysign(1.0, value) < 0 else 1
    decimal_value = Decimal(repr(abs(value)))
    _, digit_tuple, exponent = decimal_value.as_tuple()
    if not isinstance(exponent, int):  # 'n', 'N' or 'F' for special values
        raise CanonicalizationError(f"non-finite number cannot be canonicalised: {value!r}")
    digits = "".join(str(digit) for digit in digit_tuple)
    stripped = digits.rstrip("0")
    if not stripped:
        return "0", 1, sign
    exponent += len(digits) - len(stripped)
    return stripped, len(stripped) + exponent, sign


def format_number(value: float | int) -> str:
    """Format ``value`` exactly as ECMAScript's ``String(Number)`` would.

    Integers (including integral floats within the safe exponent range) are
    emitted without a decimal point, matching JavaScript, so ``1.0`` and ``1``
    hash identically. That is intentional: JSON has a single number type and a
    canonical form must not depend on the producer's static typing.
    """
    if isinstance(value, bool):  # bool is a subclass of int -- reject explicitly
        raise CanonicalizationError("booleans are not numbers in canonical JSON")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise CanonicalizationError(f"non-finite numbers cannot be canonicalised: {value!r}")
    if value == 0.0:
        # -0.0 canonicalises to "0", as String(-0) === "0" in JavaScript.
        return "0"

    digits, point, sign = _shortest_digits(value)
    k = len(digits)
    prefix = "-" if sign < 0 else ""

    if k <= point <= 21:
        return prefix + digits + "0" * (point - k)
    if 0 < point <= 21:
        return prefix + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return prefix + "0." + "0" * (-point) + digits
    # Exponential notation.
    exponent = point - 1
    sign_char = "+" if exponent >= 0 else "-"
    mantissa = digits[0] if k == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{prefix}{mantissa}e{sign_char}{abs(exponent)}"


def _canonicalize(value: Any, depth: int) -> str:
    if depth > _MAX_DEPTH:
        raise CanonicalizationError(f"structure nested deeper than {_MAX_DEPTH} levels")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _escape_string(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError(f"non-finite Decimal cannot be canonicalised: {value!r}")
        as_float = float(value)
        if Decimal(repr(as_float)).compare(value) != 0:
            # JSON numbers are IEEE-754 doubles. Silently rounding a Decimal
            # here would make the content hash depend on the producer's numeric
            # type, breaking reproducibility. Monetary values must be carried as
            # strings -- see docs/concepts/cost-attribution.md.
            raise CanonicalizationError(
                f"Decimal {value!r} is not exactly representable as a JSON number; "
                "serialise it as a string instead"
            )
        return format_number(as_float)
    if isinstance(value, (int, float)):
        return format_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonicalize(item, depth + 1) for item in value) + "]"
    if isinstance(value, dict):
        parts: list[str] = []
        for normalised, original in _sorted_keys(value):
            # The *normalised* key is emitted, the *original* is used to look
            # the value up. NFC can change a key ("שׁ" U+FB2C becomes two code
            # points), and indexing the dictionary with the normalised form
            # would raise KeyError on exactly those inputs.
            parts.append(
                f"{_escape_string(normalised)}:{_canonicalize(value[original], depth + 1)}"
            )
        return "{" + ",".join(parts) + "}"
    raise CanonicalizationError(
        f"type {type(value).__name__!r} has no canonical JSON representation; "
        "convert it to a primitive, list or dict first"
    )


def _sorted_keys(mapping: dict[Any, Any]) -> list[tuple[str, str]]:
    """Object keys as ``(normalised, original)`` pairs, in serialisation order.

    Sorted by UTF-16 code unit, which is what RFC 8785 requires. The original
    key is carried alongside because NFC normalisation may change it, and the
    value still has to be looked up under the key the caller actually used.
    """
    pairs: list[tuple[str, str]] = []
    for key in mapping:
        if not isinstance(key, str):
            raise CanonicalizationError(f"object keys must be strings, got {type(key).__name__!r}")
        pairs.append((unicodedata.normalize("NFC", key), key))
    if len({normalised for normalised, _ in pairs}) != len(pairs):
        raise CanonicalizationError("object keys collide after NFC normalisation")
    return sorted(pairs, key=lambda pair: _utf16_sort_key(pair[0]))


def _utf16_sort_key(value: str) -> tuple[int, ...]:
    """Return the UTF-16 code-unit sequence used by JavaScript string ordering.

    Python sorts ``str`` by code point, JavaScript by UTF-16 code unit. The two
    disagree for astral-plane characters (U+10000 and above sort *before*
    U+E000-U+FFFF in JavaScript because surrogates are 0xD800-0xDFFF). Sorting
    on the encoded code units makes both languages agree.
    """
    encoded = value.encode("utf-16-be")
    return tuple(
        int.from_bytes(encoded[index : index + 2], "big") for index in range(0, len(encoded), 2)
    )


def canonical_json_str(value: Any) -> str:
    """Return the RFC 8785 canonical JSON text for ``value``."""
    return _canonicalize(value, 0)


def canonical_json(value: Any) -> bytes:
    """Return the RFC 8785 canonical JSON encoding of ``value`` as UTF-8 bytes."""
    return canonical_json_str(value).encode("utf-8")


def content_hash(value: Any) -> str:
    """Return the prefixed SHA-256 content hash of ``value``.

    The prefix (``sha256:``) is part of the stored identifier so that the
    hash algorithm can be migrated without ambiguity: a future ``sha3-256:``
    hash is trivially distinguishable from a legacy one.
    """
    digest = hashlib.sha256(canonical_json(value)).hexdigest()
    return _HASH_PREFIX + digest


def verify_hash(value: Any, expected: str) -> bool:
    """Constant-time comparison of ``value``'s content hash against ``expected``."""
    import hmac

    return hmac.compare_digest(content_hash(value), expected)


def short_hash(hash_value: str, length: int = 12) -> str:
    """Return a short, human-facing form of a prefixed content hash.

    Short hashes are for display only. They are never used as lookup keys,
    because a 12-character prefix collides after roughly 2**24 values.
    """
    if not hash_value.startswith(_HASH_PREFIX):
        raise ValueError(f"expected a {_HASH_PREFIX!r} prefixed hash, got {hash_value!r}")
    if not 4 <= length <= 64:
        raise ValueError("short hash length must be between 4 and 64 characters")
    return hash_value[len(_HASH_PREFIX) : len(_HASH_PREFIX) + length]
