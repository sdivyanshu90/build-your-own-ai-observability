"""``python -m aiobs_worker`` / ``aiobs-worker`` entry point."""

from __future__ import annotations

import sys

from .main import main

__all__ = ["main"]

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
