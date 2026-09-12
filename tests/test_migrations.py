from pathlib import Path
import os
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_phase_two_registry_upgrades_and_downgrades(tmp_path) -> None:
    database_path = tmp_path / "migration" / "gateway.db"
    environment = os.environ.copy()
    environment["AIPG_DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert database_path.is_file()

    import sqlite3
    connection = sqlite3.connect(database_path)
    tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    assert {
        "model_registry",
        "provider_discovery_state",
        "gateway_api_keys",
        "harness_export_provider_preferences",
    }.issubset(tables)
    gateway_key_columns = {
        row[1] for row in connection.execute("pragma table_info(gateway_api_keys)")
    }
    assert "last_used_at" in gateway_key_columns
    connection.close()

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )



def test_phase_two_migration_excludes_credentials_usage_and_request_data(tmp_path) -> None:
    import sqlite3
    database_path = tmp_path / "migration-sensitive.db"
    environment = os.environ.copy()
    environment["AIPG_DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, env=environment, check=True, capture_output=True, text=True)
    connection = sqlite3.connect(database_path)
    tables = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
    forbidden = {"usage", "quota", "keys", "credentials", "sessions", "requests", "request_payloads"}
    assert tables.isdisjoint(forbidden)
    connection.close()
