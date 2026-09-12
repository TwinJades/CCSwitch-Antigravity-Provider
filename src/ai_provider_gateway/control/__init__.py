"""Control-plane application boundary for the local Supervisor."""

from .app import app, create_app

__all__ = ["app", "create_app"]
