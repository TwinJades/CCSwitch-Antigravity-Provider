from pathlib import Path

from sqlalchemy import text

from ai_provider_gateway.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_LOG_LEVEL,
    RuntimeSettings,
)
from ai_provider_gateway.database import create_database_engine


def test_runtime_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("AIPG_DATABASE_URL", raising=False)
    monkeypatch.delenv("AIPG_LOG_LEVEL", raising=False)

    settings = RuntimeSettings.from_env()

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.log_level == DEFAULT_LOG_LEVEL
    assert settings.sqlite_path() == Path("data/gateway.db")


def test_runtime_settings_read_environment(monkeypatch, tmp_path) -> None:
    database_path = tmp_path / "nested" / "gateway.db"
    monkeypatch.setenv("AIPG_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    monkeypatch.setenv("AIPG_LOG_LEVEL", "warning")

    settings = RuntimeSettings.from_env()

    assert settings.log_level == "WARNING"
    assert settings.sqlite_path() == database_path


def test_database_engine_creates_sqlite_parent_and_connects(tmp_path) -> None:
    database_path = tmp_path / "new" / "gateway.db"
    engine = create_database_engine(f"sqlite:///{database_path.as_posix()}")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1

    engine.dispose()
    assert database_path.is_file()
