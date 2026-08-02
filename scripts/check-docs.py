#!/usr/bin/env python
"""Validate documentation: internal links, referenced files and required pages.

CI fails on a broken link. Documentation that points at a file which no longer
exists is worse than no documentation, because the reader assumes the gap is
theirs.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"

LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

#: Pages the specification requires. Missing ones are reported by name.
REQUIRED = [
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
]


def main() -> int:
    problems: list[str] = []

    for name in REQUIRED:
        if not (ROOT / name).exists():
            problems.append(f"missing required file: {name}")

    markdown = sorted(ROOT.rglob("*.md"))
    markdown = [
        path
        for path in markdown
        if not any(part in {".venv", "node_modules", ".git", ".next"} for part in path.parts)
    ]

    checked = 0
    for path in markdown:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, target in LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            # Strip an anchor before resolving.
            clean = target.split("#", 1)[0]
            if not clean:
                continue
            resolved = (path.parent / clean).resolve()
            if not resolved.exists():
                problems.append(f"{path.relative_to(ROOT)}: broken link [{label}]({target})")

    print(f"checked {len(markdown)} documents and {checked} internal links")
    if problems:
        print(f"\n{len(problems)} problem(s):\n")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print("documentation links are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
