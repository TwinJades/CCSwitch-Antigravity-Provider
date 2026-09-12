"""Recoverable file signal used by the portable launcher shutdown control."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ShutdownRequest:
    path: Path

    def request(self) -> None:
        """Create an idempotent marker without storing credentials or request data."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as marker:
                marker.write("shutdown-requested\n")
        except FileExistsError:
            return
