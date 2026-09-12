"""Runtime configuration shared by Phase 1 process skeletons."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
import os


DEFAULT_DATABASE_URL = "sqlite:///./data/gateway.db"
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Non-secret process settings loaded from the local environment."""

    database_url: str = DEFAULT_DATABASE_URL
    log_level: str = DEFAULT_LOG_LEVEL

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        return cls(
            database_url=os.getenv("AIPG_DATABASE_URL", DEFAULT_DATABASE_URL),
            log_level=os.getenv("AIPG_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
        )

    def sqlite_path(self) -> Path | None:
        parsed = urlparse(self.database_url)
        if parsed.scheme != "sqlite" or parsed.path in {"", "/:memory:"}:
            return None
        raw_path = parsed.path.lstrip("/") if parsed.netloc == "" else parsed.path
        return Path(raw_path)
