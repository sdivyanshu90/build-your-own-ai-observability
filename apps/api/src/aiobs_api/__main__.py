"""API server entry point: ``aiobs-api``.

Runs Uvicorn with settings that matter in production and are easy to get wrong:

* ``timeout_graceful_shutdown`` bounds how long in-flight requests may take to
  finish. Without it, a rolling deploy can hang on one slow request.
* ``proxy_headers`` is enabled only when the configuration declares trusted
  proxy hops. Trusting ``X-Forwarded-For`` unconditionally lets any client spoof
  its address, which would poison rate limiting and audit records.
* ``server_header``/``date_header`` are disabled: they leak version information
  and add bytes to every response for no benefit.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .core.config import get_settings
from .core.logging import configure_logging, get_logger

log = get_logger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiobs-api", description="AI Observability API")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - containers bind all
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Uvicorn worker processes. Prefer scaling replicas over workers so "
            "each process has its own health check and lifecycle."
        ),
    )
    parser.add_argument("--reload", action="store_true", help="Development auto-reload.")
    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings)

    problems = settings.validate_for_runtime()
    if problems:
        for problem in problems:
            log.error("startup.configuration_invalid", problem=problem)
        print("refusing to start: configuration is invalid", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    import uvicorn

    uvicorn.run(
        "aiobs_api.http.app:create_app",
        factory=True,
        host=arguments.host,
        port=arguments.port,
        workers=arguments.workers if not arguments.reload else 1,
        reload=arguments.reload,
        log_config=None,  # structlog owns logging
        access_log=False,  # replaced by AccessLogMiddleware
        timeout_graceful_shutdown=int(settings.shutdown_grace_seconds),
        proxy_headers=settings.security.trusted_proxy_hops > 0,
        forwarded_allow_ips="*" if settings.security.trusted_proxy_hops > 0 else None,
        server_header=False,
        date_header=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
