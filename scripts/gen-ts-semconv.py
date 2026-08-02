#!/usr/bin/env python
"""Regenerate the TypeScript copy of the semantic-convention constants.

Three copies of these names exist -- the Python schemas package (the source of
truth), the Python SDK and the TypeScript SDK -- because neither SDK may depend
on the schemas package. A contract test asserts all three agree.

Usage:  python scripts/gen-ts-semconv.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "shared-schemas" / "python"))

import aiobs_schemas.semconv as source  # noqa: E402

TARGET = ROOT / "packages" / "shared-schemas" / "typescript" / "src" / "semconv.ts"

HEADER = """/**
 * Semantic convention constants.
 *
 * Generated from `aiobs_schemas.semconv` by `scripts/gen-ts-semconv.py`; the
 * contract test asserts the three copies (Python schemas, Python SDK, this)
 * stay identical. Do not edit by hand.
 */

"""


def main() -> int:
    names = sorted(
        name
        for name in dir(source)
        if name.isupper() and isinstance(getattr(source, name), str) and not name.startswith("_")
    )
    body = [f'export const {name} = "{getattr(source, name)}" as const;' for name in names]
    tail = [
        "",
        "/** Every constant defined here, for the parity test. */",
        "export const ALL_ATTRIBUTES: ReadonlySet<string> = new Set([",
        *[f"  {name}," for name in names],
        "]);",
        "",
    ]
    TARGET.write_text(HEADER + "\n".join(body) + "\n" + "\n".join(tail))
    print(f"wrote {len(names)} constants to {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
