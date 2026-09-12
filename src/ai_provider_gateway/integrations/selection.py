"""One persisted Provider selection shared by every Harness export."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, delete, insert, select
from sqlalchemy.engine import Engine

from .common import IntegrationConfigError, safe_id


_metadata = MetaData()
export_provider_preferences = Table(
    "harness_export_provider_preferences",
    _metadata,
    Column("provider_id", String(128), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class HarnessExportSelectionStore:
    """Persist one provider filter without changing Registry or routing state."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enabled_from(self, provider_ids: set[str]) -> frozenset[str]:
        normalized = {_provider_id(value) for value in provider_ids}
        if not normalized:
            return frozenset()
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    export_provider_preferences.c.provider_id,
                    export_provider_preferences.c.enabled,
                ).where(export_provider_preferences.c.provider_id.in_(normalized))
            ).mappings()
            saved = {row["provider_id"]: bool(row["enabled"]) for row in rows}
        return frozenset(
            provider_id
            for provider_id in normalized
            if saved.get(provider_id, True)
        )

    def replace(self, provider_ids: set[str], enabled_ids: set[str]) -> None:
        normalized = {_provider_id(value) for value in provider_ids}
        enabled = {_provider_id(value) for value in enabled_ids}
        if not enabled.issubset(normalized):
            raise IntegrationConfigError("Export selection contains an unknown provider.")
        updated_at = datetime.now(UTC)
        with self._engine.begin() as connection:
            connection.execute(delete(export_provider_preferences))
            if normalized:
                connection.execute(
                    insert(export_provider_preferences),
                    [
                        {
                            "provider_id": provider_id,
                            "enabled": provider_id in enabled,
                            "updated_at": updated_at,
                        }
                        for provider_id in sorted(normalized)
                    ],
                )


def _provider_id(value: str) -> str:
    return safe_id(value, "Provider ID", maximum=128)
