"""PyInstaller entry point for the Windows portable launcher."""

from __future__ import annotations

import sys

from ai_provider_gateway.launcher import main, show_startup_error


def run() -> int:
    try:
        return main()
    except Exception as error:
        # Service children fail silently. Their parent detects the exit and presents
        # one concise message instead of a raw packaged traceback.
        if "--service" not in sys.argv[1:]:
            show_startup_error(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
