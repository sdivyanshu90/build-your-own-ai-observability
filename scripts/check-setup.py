#!/usr/bin/env python
"""Verify the development toolchain and report what is missing.

Run before anything else. Every check prints what it found, what is required
and, when something is wrong, the exact command that fixes it -- a setup script
that says "failed" without saying what to do next has wasted the reader's time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"
BOLD = "\033[1m"

problems: list[str] = []
warnings: list[str] = []


def report(label: str, ok: bool, detail: str = "", fix: str = "", required: bool = True) -> None:
    if ok:
        print(f"  {GREEN}ok{RESET}    {label}" + (f"  ({detail})" if detail else ""))
        return
    marker = f"{RED}MISSING{RESET}" if required else f"{YELLOW}absent{RESET}"
    print(f"  {marker} {label}" + (f"  ({detail})" if detail else ""))
    if fix:
        print(f"          fix: {fix}")
    (problems if required else warnings).append(label)


def version_of(command: str, *args: str) -> str:
    binary = shutil.which(command)
    if binary is None:
        return ""
    try:
        result = subprocess.run(
            [binary, *args], capture_output=True, text=True, timeout=15, check=False
        )
        return (result.stdout or result.stderr).strip().splitlines()[0]
    except Exception:
        return ""


def main() -> int:
    print(f"\n{BOLD}Required{RESET}")
    version = sys.version_info
    report(
        "Python >= 3.10",
        version >= (3, 10),
        f"{version.major}.{version.minor}.{version.micro}",
        "install Python 3.10 or newer",
    )

    venv = ROOT / ".venv"
    report(
        "virtualenv at .venv",
        venv.exists(),
        str(venv.relative_to(ROOT)) if venv.exists() else "",
        "make install",
    )

    if venv.exists():
        interpreter = venv / "bin" / "python"
        for module, target in (
            ("aiobs_schemas", "packages/shared-schemas"),
            ("aiobs_api", "apps/api"),
            ("aiobs_worker", "apps/worker"),
            ("aiobs", "packages/python-sdk"),
            ("aiobs_providers", "packages/provider-adapters"),
        ):
            installed = (
                subprocess.run(
                    [str(interpreter), "-c", f"import {module}"],
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            )
            report(f"package {module}", installed, target, "make install")

    print(f"\n{BOLD}Optional -- needed for the full Docker stack{RESET}")
    docker = version_of("docker", "--version")
    report("docker", bool(docker), docker, "https://docs.docker.com/get-docker/", required=False)
    if docker:
        daemon = (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=30, check=False
            ).returncode
            == 0
        )
        report(
            "docker daemon running",
            daemon,
            "",
            "start Docker Desktop or `sudo systemctl start docker`",
            required=False,
        )
    compose = version_of("docker", "compose", "version")
    report("docker compose", bool(compose), compose, "", required=False)

    print(f"\n{BOLD}Optional -- needed for the frontend and TypeScript SDK{RESET}")
    node = version_of("node", "--version")
    node_ok = bool(node) and int(node.lstrip("v").split(".")[0]) >= 20
    report(
        "node >= 20",
        node_ok,
        node,
        "install Node.js 20 or newer",
        required=False,
    )
    report(
        "npm",
        bool(version_of("npm", "--version")),
        version_of("npm", "--version"),
        "",
        required=False,
    )

    print(f"\n{BOLD}Optional -- needed for load testing{RESET}")
    report(
        "k6",
        bool(shutil.which("k6")),
        "",
        "https://k6.io/docs/get-started/installation/",
        required=False,
    )

    print(f"\n{BOLD}Configuration{RESET}")
    env_file = ROOT / ".env"
    report(
        ".env present",
        env_file.exists(),
        "",
        "cp .env.example .env",
        required=False,
    )
    if os.environ.get("AIOBS_API_KEY"):
        print(f"  {GREEN}ok{RESET}    AIOBS_API_KEY is set")
    else:
        print(
            f"  {YELLOW}absent{RESET} AIOBS_API_KEY -- run `make bootstrap` and export the printed key"
        )

    print()
    if problems:
        print(f"{RED}{len(problems)} required item(s) missing:{RESET} {', '.join(problems)}")
        return 1
    if warnings:
        print(
            f"{GREEN}Ready.{RESET} {len(warnings)} optional item(s) absent: "
            f"{', '.join(warnings)} -- `make dev-local` still works."
        )
    else:
        print(f"{GREEN}Everything is available.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
