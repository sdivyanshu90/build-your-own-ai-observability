"""Framework and provider integrations.

Every integration is optional and imports its dependency lazily. Importing
``aiobs.integrations`` never fails because FastAPI or the OpenAI client is
absent -- an SDK that breaks an application's import graph is worse than one
that traces nothing.
"""

from __future__ import annotations

__all__ = ["available"]


def available() -> dict[str, bool]:
    """Report which optional integrations can be used in this environment.

    Useful in a start-up log line: "tracing enabled, openai integration
    unavailable" is a far better diagnostic than silently recording nothing.
    """
    result: dict[str, bool] = {}
    for name, module in (
        ("fastapi", "fastapi"),
        ("starlette", "starlette"),
        ("openai", "openai"),
        ("httpx", "httpx"),
    ):
        try:
            __import__(module)
            result[name] = True
        except ImportError:
            result[name] = False
    return result
