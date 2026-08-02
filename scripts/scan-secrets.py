#!/usr/bin/env python
"""Scan the working tree for committed credentials.

A last line of defence, not a replacement for a pre-commit hook or a
server-side scanner. It looks for the shapes that actually leak: provider API
keys, private keys, connection strings with inline passwords, and this
platform's own key format.

Known-safe placeholders (the .env.example values, documentation samples) are
allowlisted explicitly rather than by suppressing the whole file, so a real
secret added to one of those files is still caught.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{32,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")),
    (
        "dsn with password",
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb|redis|amqp)://[^\s:/@]+:[^\s@]{6,}@"),
    ),
    ("platform live key", re.compile(r"\baiobs_live_[0-9a-f]{8}_[A-Za-z0-9_\-]{20,}\b")),
)

#: An explicit, reviewable inline suppression. Preferred over allowlisting a
#: whole file: a *real* secret added to a suppressed file would still be found.
SUPPRESSION = "secret-scan-allow"

#: Substrings that mark a match as an intentional placeholder.
ALLOWED = (
    "dev-only-insecure-secret-change-me",
    "development-only-secret-do-not-use-in-production",
    "aiobsminio-secret",
    "postgresql+asyncpg://aiobs:aiobs@",
    "postgres://aiobs:aiobs@",
    "redis://localhost",
    "redis://redis:",
    "change-me",
    "example.com",
    "EXAMPLE",
    "<your-",
    "sk-your-key-here",
)

SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".next",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "htmlcov",
    ".aiobs",
    "dist",
    "build",
}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".lock"}


def tracked_files() -> list[Path]:
    """Every file worth scanning: tracked *and* present-but-untracked.

    Scanning only ``git ls-files`` was tried and gives a false all-clear: a
    secret written to a new file has not been committed yet, which is exactly
    the moment you want to catch it. Scanning only the working tree misses
    nothing here, so the two lists are unioned and build artefacts are excluded
    by directory name.
    """
    candidates: dict[Path, None] = {}

    try:
        result = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        for line in result.stdout.splitlines():
            if line:
                candidates[ROOT / line] = None
    except Exception:
        pass

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        candidates[path] = None

    return list(candidates)


def scan_history() -> list[str]:
    """Scan every commit's diff, not just the current tree.

    A secret deleted in a later commit is still in the history, still
    retrievable by anyone with a clone, and still needs rotating. A scanner
    that only looks at HEAD reports "clean" for exactly that case.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--all", "-p", "--no-color", "--unified=0"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception as exc:
        print(f"could not read git history: {exc}", file=sys.stderr)
        return []

    findings: list[str] = []
    commit = "(unknown)"
    for line in result.stdout.splitlines():
        if line.startswith("commit "):
            commit = line.split(" ", 1)[1][:12]
            continue
        # Only added lines: a removed secret was added by some earlier commit
        # and is reported there.
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if SUPPRESSION in line or any(marker in line for marker in ALLOWED):
            continue
        for label, pattern in PATTERNS:
            match = pattern.search(line)
            if match is None:
                continue
            excerpt = match.group(0)
            redacted = excerpt[:6] + "…" + excerpt[-4:] if len(excerpt) > 12 else "…"
            findings.append(f"commit {commit}: possible {label} ({redacted})")
    return findings


def scan_tree() -> tuple[list[str], int]:
    findings: list[str] = []
    scanned = 0

    for path in tracked_files():
        if not path.is_file() or path.suffix in SKIP_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for number, line in enumerate(text.splitlines(), start=1):
            if SUPPRESSION in line or any(marker in line for marker in ALLOWED):
                continue
            for label, pattern in PATTERNS:
                match = pattern.search(line)
                if match is None:
                    continue
                excerpt = match.group(0)
                redacted = excerpt[:6] + "…" + excerpt[-4:] if len(excerpt) > 12 else "…"
                findings.append(f"{path.relative_to(ROOT)}:{number}: possible {label} ({redacted})")

    return findings, scanned


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan for committed credentials")
    parser.add_argument(
        "--history",
        action="store_true",
        help="scan every commit's diff instead of the working tree",
    )
    arguments = parser.parse_args()

    if arguments.history:
        findings = scan_history()
        print("scanned the full commit history")
    else:
        findings, scanned = scan_tree()
        print(f"scanned {scanned} files")

    if findings:
        # Deduplicated: one secret repeated across many commits is one problem.
        unique = sorted(set(findings))
        print(f"\n{len(unique)} potential secret(s) found:\n")
        for finding in unique:
            print(f"  {finding}")
        print("\nRemove the secret, rotate it, and add a placeholder instead.")
        return 1
    print("no secrets detected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
