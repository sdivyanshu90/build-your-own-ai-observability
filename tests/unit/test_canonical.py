"""Canonical serialisation and content addressing.

These tests guard the property the whole versioning system rests on: the same
logical content always produces the same bytes, and therefore the same hash, in
every language and on every machine.
"""

from __future__ import annotations

import json
import math
import struct
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from aiobs_schemas.canonical import (
    CanonicalizationError,
    canonical_json_str,
    content_hash,
    format_number,
    short_hash,
    verify_hash,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "shared-schemas"
    / "json"
    / "number-canonicalization.json"
)


class TestNumberFormatting:
    """The subtle half: Python must reproduce ECMAScript's Number::toString."""

    def test_matches_the_javascript_fixture(self) -> None:
        document = json.loads(FIXTURE.read_text())
        mismatches = []
        for row in document["values"]:
            value = struct.unpack("<d", bytes.fromhex(row["hex"]))[0]
            actual = format_number(value)
            if actual != row["js"]:
                mismatches.append((row["js"], actual))
        assert not mismatches, f"{len(mismatches)} mismatches, first: {mismatches[:3]}"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "0"),
            (-0.0, "0"),  # String(-0) === "0"
            (1.0, "1"),  # integral floats lose the decimal point
            (1, "1"),  # ...so 1 and 1.0 hash identically
            (0.5, "0.5"),
            (1e20, "100000000000000000000"),
            (1e21, "1e+21"),  # exponential threshold
            (1e-6, "0.000001"),
            (1e-7, "1e-7"),  # the other threshold
            (123.456, "123.456"),
            (-1e21, "-1e+21"),
        ],
    )
    def test_boundary_values(self, value: float, expected: str) -> None:
        assert format_number(value) == expected

    def test_rejects_non_finite(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(CanonicalizationError):
                format_number(value)

    def test_rejects_booleans(self) -> None:
        # bool subclasses int; treating True as 1 would let `{"a": True}` and
        # `{"a": 1}` hash identically, which is wrong.
        with pytest.raises(CanonicalizationError):
            format_number(True)  # type: ignore[arg-type]


class TestCanonicalJson:
    def test_sorts_keys(self) -> None:
        assert canonical_json_str({"b": 1, "a": 2}) == '{"a":2,"b":1}'

    def test_sorts_by_utf16_code_unit(self) -> None:
        # Uppercase sorts before lowercase, and non-ASCII after both.
        assert canonical_json_str({"é": 1, "z": 2, "a": 3, "A": 4}) == '{"A":4,"a":3,"z":2,"é":1}'

    def test_key_order_does_not_affect_output(self) -> None:
        assert canonical_json_str({"a": 1, "b": 2}) == canonical_json_str({"b": 2, "a": 1})

    def test_no_insignificant_whitespace(self) -> None:
        assert " " not in canonical_json_str({"a": [1, 2], "b": {"c": 3}})

    def test_escapes_minimally(self) -> None:
        assert canonical_json_str({"k": 'a\nb\tc"d\\e'}) == '{"k":"a\\nb\\tc\\"d\\\\e"}'

    def test_does_not_escape_printable_unicode(self) -> None:
        # \uXXXX escaping of printable characters would still be valid JSON but
        # would not be canonical, and would differ from the JS implementation.
        assert canonical_json_str({"k": "héllo →"}) == '{"k":"héllo →"}'

    def test_nfc_normalises_strings(self) -> None:
        composed = "é"  # U+00E9
        decomposed = "é"  # U+0065 U+0301
        assert canonical_json_str({"k": composed}) == canonical_json_str({"k": decomposed})

    def test_rejects_keys_colliding_after_normalisation(self) -> None:
        with pytest.raises(CanonicalizationError, match="collide"):
            canonical_json_str({"é": 1, "é": 2})

    def test_rejects_non_string_keys(self) -> None:
        with pytest.raises(CanonicalizationError, match="keys must be strings"):
            canonical_json_str({1: "a"})

    def test_rejects_unrepresentable_types(self) -> None:
        with pytest.raises(CanonicalizationError, match="no canonical JSON"):
            canonical_json_str({"k": object()})

    def test_rejects_excessive_nesting(self) -> None:
        deep: dict = {}
        cursor = deep
        for _ in range(70):
            cursor["n"] = {}
            cursor = cursor["n"]
        with pytest.raises(CanonicalizationError, match="nested deeper"):
            canonical_json_str(deep)

    def test_exact_decimal_is_accepted(self) -> None:
        assert canonical_json_str({"t": Decimal("0.5")}) == '{"t":0.5}'

    def test_inexact_decimal_is_rejected(self) -> None:
        # Silently rounding here would make the hash depend on whether the
        # producer used Decimal or float.
        with pytest.raises(CanonicalizationError, match="not exactly representable"):
            canonical_json_str({"t": Decimal("0.1234567890123456789012345")})


class TestContentHash:
    def test_is_prefixed(self) -> None:
        assert content_hash({"a": 1}).startswith("sha256:")

    def test_is_stable_across_key_order(self) -> None:
        assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})

    def test_differs_for_different_content(self) -> None:
        assert content_hash({"a": 1}) != content_hash({"a": 2})

    def test_int_and_integral_float_hash_identically(self) -> None:
        # JSON has one number type; the canonical form must not depend on the
        # producer's static typing.
        assert content_hash({"a": 1}) == content_hash({"a": 1.0})

    def test_verify_hash_round_trip(self) -> None:
        value = {"messages": [{"role": "system", "content": "hi"}]}
        assert verify_hash(value, content_hash(value))
        assert not verify_hash({"messages": []}, content_hash(value))

    def test_short_hash_requires_a_prefixed_input(self) -> None:
        with pytest.raises(ValueError, match="prefixed hash"):
            short_hash("deadbeef")

    def test_short_hash_length_is_bounded(self) -> None:
        digest = content_hash({"a": 1})
        assert len(short_hash(digest)) == 12
        with pytest.raises(ValueError):
            short_hash(digest, length=2)


# ---------------------------------------------------------------------------
# property-based
# ---------------------------------------------------------------------------

json_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.floats(allow_nan=False, allow_infinity=False, width=64),
    st.text(max_size=40),
)

json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=5),
    ),
    max_leaves=15,
)


class TestUnicodeKeys:
    """Keys that change shape under NFC normalisation.

    Found by the property suite: U+FB2C (Hebrew letter shin with dagesh and
    shin dot) normalises to three code points. The canonicaliser emitted the
    normalised key but looked the value up under it, so every object with such
    a key raised ``KeyError`` instead of hashing.
    """

    def test_a_key_that_changes_under_nfc_still_serialises(self) -> None:
        # U+FB2C is a single code point that NFC expands to three.
        assert canonical_json_str({"\ufb2c": None}) == '{"\u05e9\u05bc\u05c1":null}'

    def test_the_emitted_key_is_the_normalised_form(self) -> None:
        text = canonical_json_str({"\ufb2c": 1})
        assert "\ufb2c" not in text
        assert json.loads(text) == {"\u05e9\u05bc\u05c1": 1}

    def test_composed_and_decomposed_keys_hash_identically(self) -> None:
        composed = {"\u00e9": 1}  # é
        decomposed = {"e\u0301": 1}  # e + combining acute
        assert canonical_json_str(composed) == canonical_json_str(decomposed)

    def test_keys_colliding_after_normalisation_are_rejected(self) -> None:
        with pytest.raises(CanonicalizationError):
            canonical_json_str({"\u00e9": 1, "e\u0301": 2})

    def test_values_are_preserved_for_normalising_keys(self) -> None:
        # The lookup must use the original key, so the value must survive.
        assert json.loads(canonical_json_str({"\ufb2c": "kept"})) == {"\u05e9\u05bc\u05c1": "kept"}


class TestCanonicalProperties:
    @given(value=json_values)
    @hypothesis_settings(max_examples=200, deadline=None)
    def test_canonicalisation_is_deterministic(self, value: object) -> None:
        try:
            first = canonical_json_str(value)
        except CanonicalizationError:
            return
        assert canonical_json_str(value) == first

    @given(value=json_values)
    @hypothesis_settings(max_examples=200, deadline=None)
    def test_output_is_parseable_json(self, value: object) -> None:
        try:
            text = canonical_json_str(value)
        except CanonicalizationError:
            return
        json.loads(text)

    @given(value=json_values)
    @hypothesis_settings(max_examples=200, deadline=None)
    def test_round_trip_preserves_semantics(self, value: object) -> None:
        """Canonicalising a value, parsing it and canonicalising again is a
        fixed point -- which is what makes the hash reproducible after data has
        been through storage."""
        try:
            text = canonical_json_str(value)
        except CanonicalizationError:
            return
        assert canonical_json_str(json.loads(text)) == text

    @given(
        value=st.floats(allow_nan=False, allow_infinity=False, width=64).filter(
            lambda item: item == item and abs(item) < 1e300
        )
    )
    @hypothesis_settings(max_examples=300, deadline=None)
    def test_number_formatting_round_trips(self, value: float) -> None:
        """The formatted text must parse back to the identical double, or the
        shortest-representation guarantee is broken."""
        assert float(format_number(value)) == value or math.isclose(
            float(format_number(value)), value, rel_tol=0, abs_tol=0
        )
