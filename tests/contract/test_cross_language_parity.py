"""Cross-language contract tests.

Three copies of the semantic-convention constants exist -- the Python schemas
package, the Python SDK and the TypeScript SDK -- because neither SDK may
depend on the schemas package. These tests are what keep them identical.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class TestSemanticConventionParity:
    def test_python_sdk_matches_the_schemas_package(self) -> None:
        import aiobs.semconv as sdk
        import aiobs_schemas.semconv as source

        expected = {
            name: getattr(source, name)
            for name in dir(source)
            if name.isupper()
            and isinstance(getattr(source, name), str)
            and not name.startswith("_")
        }
        actual = {
            name: getattr(sdk, name)
            for name in dir(sdk)
            if name.isupper()
            and isinstance(getattr(sdk, name), str)
            and not name.startswith("_")
            and name != "ALL_ATTRIBUTES"
        }
        assert actual == expected, (
            "the SDK's semconv copy has drifted; run scripts/gen-sdk-semconv.py"
        )

    def test_typescript_matches_the_schemas_package(self) -> None:
        import aiobs_schemas.semconv as source

        target = ROOT / "packages/shared-schemas/typescript/src/semconv.ts"
        text = target.read_text()
        # `\s*` across the assignment: Prettier wraps a long declaration onto
        # two lines, and a regex anchored to one line silently sees fewer
        # constants than exist -- which reads as drift rather than as a
        # formatting change.
        found = dict(re.findall(r'export const (\w+) =\s*"([^"]+)" as const;', text))
        expected = {
            name: getattr(source, name)
            for name in dir(source)
            if name.isupper()
            and isinstance(getattr(source, name), str)
            and not name.startswith("_")
        }
        assert found == expected, (
            "the TypeScript semconv copy has drifted; run scripts/gen-ts-semconv.py"
        )

    def test_every_registered_attribute_has_a_constant(self) -> None:
        import aiobs_schemas.semconv as source

        constants = {
            getattr(source, name)
            for name in dir(source)
            if name.isupper() and isinstance(getattr(source, name), str)
        }
        missing = set(source.REGISTRY) - constants
        assert not missing, f"registry entries with no constant: {sorted(missing)}"

    def test_sensitive_attributes_are_declared_deliberately(self) -> None:
        """A sensitive attribute must not be one the platform needs verbatim."""
        import aiobs_schemas.semconv as source

        must_not_be_sensitive = {
            source.USAGE_INPUT_TOKENS,
            source.USAGE_OUTPUT_TOKENS,
            source.USAGE_TOTAL_TOKENS,
            source.COST_TOTAL,
            source.SESSION_ID,
            source.SUBJECT_ID,
            source.SPAN_CATEGORY,
        }
        overlap = must_not_be_sensitive & source.SENSITIVE_ATTRIBUTES
        assert not overlap, f"these attributes must not be redacted: {sorted(overlap)}"


class TestNumberFixture:
    """The canonical-number contract, verified from both languages."""

    def test_python_reproduces_every_fixture_value(self) -> None:
        import struct

        from aiobs_schemas.canonical import format_number

        document = json.loads(
            (ROOT / "packages/shared-schemas/json/number-canonicalization.json").read_text()
        )
        for row in document["values"]:
            value = struct.unpack("<d", bytes.fromhex(row["hex"]))[0]
            assert format_number(value) == row["js"]

    @pytest.mark.skipif(
        not (ROOT / "packages/shared-schemas/typescript/dist/canonical.js").exists(),
        reason="build the TypeScript package first: npm run build",
    )
    def test_typescript_and_python_agree_on_a_content_hash(self) -> None:
        """The property that matters: the same prompt hashed in either language
        produces the same version identifier."""
        from aiobs_schemas.canonical import content_hash

        sample = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "héllo → world"},
            ],
            "temperature": 0.7,
            "max_tokens": 2048,
            "nested": {"z": [1, 2.5, True, None], "a": "é"},
        }
        script = (
            "import {contentHash} from "
            "'./packages/shared-schemas/typescript/dist/canonical.js';"
            f"console.log(await contentHash({json.dumps(sample)}));"
        )
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == content_hash(sample)
