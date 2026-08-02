#!/usr/bin/env python
"""Print the OpenAPI schema to stdout.

Used by `make openapi` and by CI's contract-drift check: the committed
openapi.json must match what the code generates, so an accidental change to a
response model fails the build instead of silently breaking a generated client.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    from aiobs_api.core.config import Settings
    from aiobs_api.http.app import create_app

    app = create_app(Settings())
    json.dump(app.openapi(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
