"""Filter, sort and pagination grammar.

Every list endpoint in the API speaks the same query language, and it is
declared here once rather than reimplemented per router.

Filter grammar
--------------
``?filter=<field>:<op>:<value>``, repeatable. Example::

    ?filter=status:eq:error
    &filter=duration_ms:gte:500
    &filter=model:in:gpt-4o|claude-sonnet-4
    &filter=tags:has:production
    &filter=start_time:between:2026-01-01T00:00:00Z|2026-01-02T00:00:00Z

The colon-delimited form was chosen over a free-form expression language for
one reason: **it is trivially validatable**. Every field name is looked up in a
per-resource :class:`FieldSpec` registry before it reaches a query builder, so
an unknown field is a 400 rather than an opportunity to inject SQL. Operators
are a closed enum. Values are coerced to the declared type. A query builder
therefore only ever sees ``(known_column, known_operator, typed_value)``.

Sort grammar
------------
``?sort=-start_time,duration_ms`` -- comma separated, leading ``-`` for
descending. Sortable fields are declared per resource; an implicit unique
tiebreaker is always appended so pagination is stable.

Pagination
----------
Keyset (cursor) pagination, not offset. ``OFFSET 100000`` makes the database
read and discard 100,000 rows; a keyset predicate seeks straight to the page.
Cursors are opaque, HMAC-signed base64: clients cannot forge one to smuggle a
different filter, and a truncated or edited cursor produces a clean
``invalid_cursor`` error instead of a confusing result set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Generic, TypeVar

from .errors import ValidationFailedError
from .timeutil import parse_rfc3339

__all__ = [
    "CursorCodec",
    "FieldSpec",
    "FieldType",
    "FilterCondition",
    "FilterOperator",
    "Page",
    "PageRequest",
    "ResourceSchema",
    "SortDirection",
    "SortTerm",
    "parse_filters",
    "parse_sort",
]

T = TypeVar("T")


class FieldType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"
    #: A string array column; supports ``has``/``has_any``.
    STRING_ARRAY = "string_array"
    #: A JSON/map column addressed as ``attributes.some.key``.
    MAP = "map"


class FilterOperator(str, Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    ENDS_WITH = "ends_with"
    #: Array membership.
    HAS = "has"
    HAS_ANY = "has_any"
    HAS_ALL = "has_all"
    BETWEEN = "between"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


_OPERATORS_BY_TYPE: dict[FieldType, frozenset[FilterOperator]] = {
    FieldType.STRING: frozenset(
        {
            FilterOperator.EQ,
            FilterOperator.NE,
            FilterOperator.IN,
            FilterOperator.NOT_IN,
            FilterOperator.CONTAINS,
            FilterOperator.STARTS_WITH,
            FilterOperator.ENDS_WITH,
            FilterOperator.IS_NULL,
            FilterOperator.IS_NOT_NULL,
        }
    ),
    FieldType.INTEGER: frozenset(
        {
            FilterOperator.EQ,
            FilterOperator.NE,
            FilterOperator.GT,
            FilterOperator.GTE,
            FilterOperator.LT,
            FilterOperator.LTE,
            FilterOperator.IN,
            FilterOperator.NOT_IN,
            FilterOperator.BETWEEN,
            FilterOperator.IS_NULL,
            FilterOperator.IS_NOT_NULL,
        }
    ),
    FieldType.BOOLEAN: frozenset({FilterOperator.EQ, FilterOperator.NE}),
    FieldType.TIMESTAMP: frozenset(
        {
            FilterOperator.EQ,
            FilterOperator.GT,
            FilterOperator.GTE,
            FilterOperator.LT,
            FilterOperator.LTE,
            FilterOperator.BETWEEN,
        }
    ),
    FieldType.STRING_ARRAY: frozenset(
        {FilterOperator.HAS, FilterOperator.HAS_ANY, FilterOperator.HAS_ALL}
    ),
    FieldType.MAP: frozenset(
        {
            FilterOperator.EQ,
            FilterOperator.NE,
            FilterOperator.CONTAINS,
            FilterOperator.IS_NULL,
            FilterOperator.IS_NOT_NULL,
        }
    ),
}
_OPERATORS_BY_TYPE[FieldType.NUMBER] = _OPERATORS_BY_TYPE[FieldType.INTEGER]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """A field that may appear in a filter or sort clause.

    ``column`` is the physical column or expression the query builder emits. It
    is never derived from user input, which is what makes the whole grammar
    injection-safe.
    """

    name: str
    type: FieldType
    column: str
    filterable: bool = True
    sortable: bool = False
    #: When set, the value is validated against this closed set.
    allowed_values: frozenset[str] | None = None
    description: str = ""
    #: Map fields accept a dotted suffix, e.g. ``attributes.gen_ai.request.model``.
    supports_subpath: bool = False

    def supported_operators(self) -> frozenset[FilterOperator]:
        return _OPERATORS_BY_TYPE[self.type]


@dataclass(frozen=True, slots=True)
class FilterCondition:
    """One validated ``field op value`` triple."""

    field: FieldSpec
    operator: FilterOperator
    #: Coerced value: scalar, list (for ``in``/``has_*``), or 2-tuple (``between``).
    value: Any
    #: Sub-path for MAP fields, e.g. ``gen_ai.request.model``.
    subpath: str | None = None

    @property
    def column(self) -> str:
        return self.field.column


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class SortTerm:
    field: FieldSpec
    direction: SortDirection


@dataclass(frozen=True, slots=True)
class ResourceSchema:
    """The set of fields a particular list endpoint exposes."""

    name: str
    fields: tuple[FieldSpec, ...]
    #: Applied when the client supplies no ``sort``.
    default_sort: tuple[SortTerm, ...] = ()
    #: A field that is unique per row, appended to every sort so keyset
    #: pagination never skips or repeats rows that share a sort value.
    tiebreaker: str = "id"

    def field_map(self) -> dict[str, FieldSpec]:
        return {field.name: field for field in self.fields}

    def get(self, name: str) -> FieldSpec | None:
        return self.field_map().get(name)


def _coerce_scalar(field: FieldSpec, raw: str) -> Any:
    """Coerce a raw query-string token to the field's declared type."""
    try:
        if field.type in {FieldType.STRING, FieldType.MAP}:
            if field.allowed_values is not None and raw not in field.allowed_values:
                raise ValidationFailedError(
                    f"filter value {raw!r} is not permitted for field {field.name!r}; "
                    f"allowed: {sorted(field.allowed_values)}"
                )
            return raw
        if field.type is FieldType.INTEGER:
            return int(raw)
        if field.type is FieldType.NUMBER:
            return float(raw)
        if field.type is FieldType.BOOLEAN:
            lowered = raw.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
            raise ValueError(raw)
        if field.type is FieldType.TIMESTAMP:
            return parse_rfc3339(raw)
        if field.type is FieldType.STRING_ARRAY:
            return raw
    except ValidationFailedError:
        raise
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError(
            f"filter value {raw!r} is not a valid {field.type.value} for field {field.name!r}"
        ) from exc
    raise ValidationFailedError(f"unsupported field type for {field.name!r}")


#: Guards against a client sending thousands of filters to force a pathological
#: query plan.
MAX_FILTERS = 25
MAX_IN_VALUES = 200

#: Characters permitted in a map sub-path such as ``attributes.gen_ai.request.model``.
_SUBPATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,255}$")


def parse_filters(
    schema: ResourceSchema, raw_filters: Sequence[str]
) -> tuple[FilterCondition, ...]:
    """Parse and validate repeated ``filter`` query parameters.

    Raises :class:`ValidationFailedError` with an actionable message on anything
    unknown. Silently ignoring an unrecognised filter would be worse than
    failing: the client would believe its query was narrowed when it was not.
    """
    if len(raw_filters) > MAX_FILTERS:
        raise ValidationFailedError(f"at most {MAX_FILTERS} filters may be supplied")

    field_map = schema.field_map()
    conditions: list[FilterCondition] = []

    for raw in raw_filters:
        parts = raw.split(":", 2)
        if len(parts) < 2:
            raise ValidationFailedError(
                f"malformed filter {raw!r}: expected '<field>:<operator>' or "
                "'<field>:<operator>:<value>'"
            )
        field_token, operator_token = parts[0], parts[1]
        value_token = parts[2] if len(parts) == 3 else ""

        subpath: str | None = None
        field = field_map.get(field_token)
        if field is None and "." in field_token:
            root, _, remainder = field_token.partition(".")
            candidate = field_map.get(root)
            if candidate is not None and candidate.supports_subpath:
                # The sub-path becomes part of a JSON path expression in the
                # analytics driver. Restricting it to attribute-name characters
                # keeps that expression unforgeable.
                if not _SUBPATH_RE.match(remainder):
                    raise ValidationFailedError(
                        f"attribute path {remainder!r} may only contain letters, "
                        "digits, dots, dashes and underscores"
                    )
                field, subpath = candidate, remainder
        if field is None:
            raise ValidationFailedError(
                f"unknown filter field {field_token!r} for {schema.name}; "
                f"available: {sorted(field_map)}"
            )
        if not field.filterable:
            raise ValidationFailedError(f"field {field.name!r} is not filterable")

        try:
            operator = FilterOperator(operator_token)
        except ValueError as exc:
            raise ValidationFailedError(
                f"unknown filter operator {operator_token!r}; "
                f"available: {[op.value for op in FilterOperator]}"
            ) from exc
        if operator not in field.supported_operators():
            raise ValidationFailedError(
                f"operator {operator.value!r} is not supported for "
                f"{field.type.value} field {field.name!r}; "
                f"supported: {sorted(op.value for op in field.supported_operators())}"
            )

        value: Any
        if operator in {FilterOperator.IS_NULL, FilterOperator.IS_NOT_NULL}:
            value = None
        elif operator in {
            FilterOperator.IN,
            FilterOperator.NOT_IN,
            FilterOperator.HAS_ANY,
            FilterOperator.HAS_ALL,
        }:
            tokens = [token for token in value_token.split("|") if token != ""]
            if not tokens:
                raise ValidationFailedError(
                    f"operator {operator.value!r} needs at least one '|'-separated value"
                )
            if len(tokens) > MAX_IN_VALUES:
                raise ValidationFailedError(
                    f"at most {MAX_IN_VALUES} values may be supplied to {operator.value!r}"
                )
            value = [_coerce_scalar(field, token) for token in tokens]
        elif operator is FilterOperator.BETWEEN:
            tokens = value_token.split("|")
            if len(tokens) != 2:
                raise ValidationFailedError(
                    "operator 'between' needs exactly two '|'-separated bounds"
                )
            low, high = (_coerce_scalar(field, token) for token in tokens)
            if low > high:
                raise ValidationFailedError("'between' lower bound exceeds the upper bound")
            value = (low, high)
        else:
            value = _coerce_scalar(field, value_token)

        conditions.append(
            FilterCondition(field=field, operator=operator, value=value, subpath=subpath)
        )

    return tuple(conditions)


MAX_SORT_TERMS = 4


def parse_sort(schema: ResourceSchema, raw: str | None) -> tuple[SortTerm, ...]:
    """Parse a ``sort`` parameter, falling back to the schema default."""
    if not raw:
        return schema.default_sort

    field_map = schema.field_map()
    terms: list[SortTerm] = []
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if len(tokens) > MAX_SORT_TERMS:
        raise ValidationFailedError(f"at most {MAX_SORT_TERMS} sort terms may be supplied")

    seen: set[str] = set()
    for token in tokens:
        direction = SortDirection.ASC
        name = token
        if token.startswith("-"):
            direction, name = SortDirection.DESC, token[1:]
        elif token.startswith("+"):
            name = token[1:]
        field = field_map.get(name)
        if field is None:
            raise ValidationFailedError(
                f"unknown sort field {name!r} for {schema.name}; "
                f"sortable: {sorted(f.name for f in schema.fields if f.sortable)}"
            )
        if not field.sortable:
            raise ValidationFailedError(f"field {field.name!r} is not sortable")
        if field.name in seen:
            raise ValidationFailedError(f"sort field {field.name!r} appears more than once")
        seen.add(field.name)
        terms.append(SortTerm(field=field, direction=direction))

    return tuple(terms)


class CursorCodec:
    """Encodes and decodes signed, opaque pagination cursors.

    The signature is not about confidentiality -- the payload is only sort-key
    values -- but about integrity. An unsigned cursor is user-controlled input
    spliced straight into a WHERE clause; signing it means a tampered cursor is
    rejected before it ever reaches the query builder.
    """

    __slots__ = ("_key",)

    def __init__(self, secret: str) -> None:
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def encode(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=_json_default)
        raw = body.encode("utf-8")
        signature = hmac.new(self._key, raw, hashlib.sha256).digest()[:16]
        return base64.urlsafe_b64encode(signature + raw).decode("ascii").rstrip("=")

    def decode(self, cursor: str) -> dict[str, Any]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            blob = base64.urlsafe_b64decode(padded.encode("ascii"))
        except Exception as exc:
            raise ValidationFailedError("pagination cursor is malformed") from exc
        if len(blob) <= 16:
            raise ValidationFailedError("pagination cursor is malformed")
        signature, raw = blob[:16], blob[16:]
        expected = hmac.new(self._key, raw, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(signature, expected):
            raise ValidationFailedError("pagination cursor failed integrity validation")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValidationFailedError("pagination cursor is malformed") from exc
        if not isinstance(payload, dict):
            raise ValidationFailedError("pagination cursor is malformed")
        return payload


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, Decimal):
        # Sorting by cost puts a Decimal in the keyset. It is tagged and carried
        # as its exact string form -- encoding it as a JSON number would round a
        # per-token price through a float and could place the cursor on the
        # wrong side of a row boundary, silently skipping or repeating results.
        return {"__dec__": str(value)}
    raise TypeError(f"{type(value).__name__} is not cursor-serialisable")


def revive_cursor_values(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore values encoded by :func:`_json_default`."""
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, dict) and "__dt__" in value:
            result[key] = parse_rfc3339(value["__dt__"])
        elif isinstance(value, dict) and "__dec__" in value:
            try:
                result[key] = Decimal(str(value["__dec__"]))
            except InvalidOperation as exc:
                raise ValidationFailedError("pagination cursor is malformed") from exc
        else:
            result[key] = value
    return result


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500


@dataclass(frozen=True, slots=True)
class PageRequest:
    """A validated pagination request."""

    limit: int = DEFAULT_PAGE_SIZE
    cursor: dict[str, Any] | None = None

    @classmethod
    def build(
        cls,
        codec: CursorCodec,
        *,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> PageRequest:
        effective = DEFAULT_PAGE_SIZE if limit is None else limit
        if effective < 1 or effective > MAX_PAGE_SIZE:
            raise ValidationFailedError(
                f"limit must be between 1 and {MAX_PAGE_SIZE}, got {effective}"
            )
        decoded = revive_cursor_values(codec.decode(cursor)) if cursor else None
        return cls(limit=effective, cursor=decoded)


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of results plus the cursor needed to fetch the next one.

    There is deliberately no ``total`` field. Counting matched rows in a
    100-billion-row analytics table costs as much as the query itself, and the
    number is stale the moment it is computed. Endpoints that genuinely need a
    count expose a separate, explicitly-approximate ``/count`` operation.
    """

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[T],
        *,
        limit: int,
        codec: CursorCodec,
        cursor_for: Callable[[T], dict[str, Any]],
    ) -> Page[T]:
        """Build a page from ``limit + 1`` fetched rows.

        Fetching one extra row is how ``has_more`` is determined without a
        second query or a count.
        """
        has_more = len(rows) > limit
        items = list(rows[:limit])
        next_cursor = codec.encode(cursor_for(items[-1])) if has_more and items else None
        return cls(items=items, next_cursor=next_cursor, has_more=has_more)

    def map(self, transform: Callable[[T], Any]) -> Page[Any]:
        """Return a page with each item transformed, preserving cursors."""
        return Page(
            items=[transform(item) for item in self.items],
            next_cursor=self.next_cursor,
            has_more=self.has_more,
        )


def build_schema(
    name: str,
    fields: Iterable[FieldSpec],
    *,
    default_sort: Sequence[tuple[str, SortDirection]] = (),
    tiebreaker: str = "id",
) -> ResourceSchema:
    """Convenience constructor that resolves default-sort field names."""
    field_tuple = tuple(fields)
    lookup = {field.name: field for field in field_tuple}
    terms: list[SortTerm] = []
    for field_name, direction in default_sort:
        field = lookup.get(field_name)
        if field is None:
            raise KeyError(f"default sort references unknown field {field_name!r}")
        terms.append(SortTerm(field=field, direction=direction))
    return ResourceSchema(
        name=name,
        fields=field_tuple,
        default_sort=tuple(terms),
        tiebreaker=tiebreaker,
    )
