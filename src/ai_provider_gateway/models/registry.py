"""Transactional storage for dynamically discovered provider models.

This module deliberately has no HTTP or Sidecar dependency.  A discovery
adapter supplies its observed models, while the registry makes the resulting
catalogue durable and safe to consume in a later Gateway API phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import re
from typing import Iterable

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError


class ModelRegistryError(ValueError):
    """Raised when registry input would violate the stable catalogue contract."""


class DiscoveryState(StrEnum):
    """The quality of the latest attempt to discover a provider catalogue."""

    AVAILABLE = "available"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    """One model as observed from a provider's current, dynamic catalogue."""

    upstream_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelRecord:
    """A stable local representation of an upstream model."""

    canonical_id: str
    provider: str
    upstream_id: str
    display_name: str | None
    alias: str | None
    available: bool
    discovered_at: datetime
    discovery_state: DiscoveryState
    last_success_at: datetime | None


@dataclass(frozen=True, slots=True)
class DiscoverySnapshot:
    """Provider-level freshness metadata for the durable catalogue."""

    provider: str
    state: DiscoveryState
    last_success_at: datetime | None
    last_attempt_at: datetime
    error_category: str | None


_metadata = MetaData()

model_registry = Table(
    "model_registry",
    _metadata,
    Column("canonical_id", String(512), primary_key=True),
    Column("provider", String(128), nullable=False),
    Column("upstream_id", String(512), nullable=False),
    Column("display_name", String(512), nullable=True),
    Column("alias", String(512), nullable=True, unique=True),
    Column("available", Boolean, nullable=False),
    Column("discovered_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("provider", "upstream_id", name="uq_model_registry_provider_upstream"),
)
Index("ix_model_registry_provider", model_registry.c.provider)
Index("ix_model_registry_available", model_registry.c.available)

provider_discovery_state = Table(
    "provider_discovery_state",
    _metadata,
    Column("provider", String(128), primary_key=True),
    Column("state", String(32), nullable=False),
    Column("last_success_at", DateTime(timezone=True), nullable=True),
    Column("last_attempt_at", DateTime(timezone=True), nullable=False),
    Column("error_category", String(64), nullable=True),
)

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_ERROR_CATEGORY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


def canonical_model_id(provider: str, upstream_id: str) -> str:
    """Build a globally stable ID in the required ``provider/upstream_id`` form."""

    return f"{_normalise_provider(provider)}/{_normalise_upstream_id(upstream_id)}"


class ModelRegistry:
    """SQLAlchemy Core registry which owns only model-catalogue persistence."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def refresh_success(self, provider: str, models: Iterable[DiscoveredModel]) -> list[ModelRecord]:
        """Atomically apply a non-empty discovery result for one provider.

        Models missing from this successful result remain stored but become
        unavailable.  An empty result is treated as a failed discovery, so it
        cannot erase a previously useful catalogue.
        """

        normalized_provider = _normalise_provider(provider)
        normalized_models = _validate_models(normalized_provider, models)
        if not normalized_models:
            self.record_failure(normalized_provider, "empty_catalog")
            raise ModelRegistryError("empty model catalogues are not successful discoveries")

        attempted_at = _utc_now()
        with self._engine.begin() as connection:
            return self._refresh_success_in_transaction(
                connection, normalized_provider, normalized_models, attempted_at
            )

    def refresh_success_in_transaction(
        self,
        connection: Connection,
        provider: str,
        models: Iterable[DiscoveredModel],
    ) -> list[ModelRecord]:
        """Apply a catalogue through a caller-owned transaction.

        This is used when provider connection metadata and its model catalogue
        must become visible atomically. Empty catalogues raise without writing a
        separate failure snapshot because the caller owns the transaction.
        """

        normalized_provider = _normalise_provider(provider)
        normalized_models = _validate_models(normalized_provider, models)
        if not normalized_models:
            raise ModelRegistryError("empty model catalogues are not successful discoveries")
        return self._refresh_success_in_transaction(
            connection, normalized_provider, normalized_models, _utc_now()
        )

    def refresh_configured_in_transaction(
        self,
        connection: Connection,
        provider: str,
        models: Iterable[DiscoveredModel],
    ) -> list[ModelRecord]:
        """Store a manually configured catalogue without claiming live verification."""

        normalized_provider = _normalise_provider(provider)
        normalized_models = _validate_models(normalized_provider, models)
        if not normalized_models:
            raise ModelRegistryError("empty model catalogues are not valid configurations")
        attempted_at = _utc_now()
        previous_success = connection.execute(
            select(provider_discovery_state.c.last_success_at).where(
                provider_discovery_state.c.provider == normalized_provider
            )
        ).scalar_one_or_none()
        records = self._refresh_success_in_transaction(
            connection, normalized_provider, normalized_models, attempted_at
        )
        self._upsert_discovery_state(
            connection,
            provider=normalized_provider,
            state=DiscoveryState.STALE,
            last_success_at=previous_success,
            last_attempt_at=attempted_at,
            error_category="not_verified",
        )
        return records

    def _refresh_success_in_transaction(
        self,
        connection: Connection,
        normalized_provider: str,
        normalized_models: list[DiscoveredModel],
        attempted_at: datetime,
    ) -> list[ModelRecord]:
        connection.execute(
            update(model_registry)
            .where(model_registry.c.provider == normalized_provider)
            .values(available=False)
        )
        for discovered in normalized_models:
            canonical_id = canonical_model_id(normalized_provider, discovered.upstream_id)
            existing = connection.execute(
                select(model_registry.c.canonical_id).where(
                    model_registry.c.canonical_id == canonical_id
                )
            ).first()
            values = {
                "provider": normalized_provider,
                "upstream_id": discovered.upstream_id,
                "display_name": discovered.display_name,
                "available": True,
                "discovered_at": attempted_at,
            }
            if existing:
                connection.execute(
                    update(model_registry)
                    .where(model_registry.c.canonical_id == canonical_id)
                    .values(**values)
                )
            else:
                connection.execute(
                    insert(model_registry).values(canonical_id=canonical_id, alias=None, **values)
                )
        self._upsert_discovery_state(
            connection,
            provider=normalized_provider,
            state=DiscoveryState.AVAILABLE,
            last_success_at=attempted_at,
            last_attempt_at=attempted_at,
            error_category=None,
        )
        rows = connection.execute(
            select(
                model_registry,
                provider_discovery_state.c.state.label("discovery_state"),
                provider_discovery_state.c.last_success_at.label("last_success_at"),
            )
            .select_from(
                model_registry.join(
                    provider_discovery_state,
                    model_registry.c.provider == provider_discovery_state.c.provider,
                )
            )
            .where(model_registry.c.provider == normalized_provider)
            .order_by(model_registry.c.canonical_id)
        ).mappings()
        return [_model_record(row) for row in rows]

    def record_failure(self, provider: str, error_category: str) -> None:
        """Record a safe failure category without changing existing models."""

        normalized_provider = _normalise_provider(provider)
        safe_category = _normalise_error_category(error_category)
        attempted_at = _utc_now()
        with self._engine.begin() as connection:
            previous = connection.execute(
                select(provider_discovery_state.c.last_success_at).where(
                    provider_discovery_state.c.provider == normalized_provider
                )
            ).scalar_one_or_none()
            self._upsert_discovery_state(
                connection,
                provider=normalized_provider,
                state=DiscoveryState.STALE,
                last_success_at=previous,
                last_attempt_at=attempted_at,
                error_category=safe_category,
            )

    def set_alias(self, canonical_id: str, alias: str | None) -> ModelRecord:
        """Set or clear an optional globally unique user-facing alias."""

        normalized_id = _normalise_canonical_id(canonical_id)
        normalized_alias = _normalise_alias(alias)
        with self._engine.begin() as connection:
            if normalized_alias is not None:
                collision = connection.execute(
                    select(model_registry.c.canonical_id).where(
                        model_registry.c.alias == normalized_alias,
                        model_registry.c.canonical_id != normalized_id,
                    )
                ).first()
                if collision:
                    raise ModelRegistryError("model alias is already assigned")
            try:
                result = connection.execute(
                    update(model_registry)
                    .where(model_registry.c.canonical_id == normalized_id)
                    .values(alias=normalized_alias)
                )
            except IntegrityError as exc:
                raise ModelRegistryError("model alias is already assigned") from exc
            if result.rowcount != 1:
                raise ModelRegistryError("unknown canonical model ID")
            row = connection.execute(
                select(
                    model_registry,
                    provider_discovery_state.c.state.label("discovery_state"),
                    provider_discovery_state.c.last_success_at.label("last_success_at"),
                )
                .select_from(
                    model_registry.join(
                        provider_discovery_state,
                        model_registry.c.provider == provider_discovery_state.c.provider,
                    )
                )
                .where(model_registry.c.canonical_id == normalized_id)
            ).mappings().one()
            return _model_record(row)

    def list_models(self, provider: str | None = None) -> list[ModelRecord]:
        """Return the durable catalogue; callers may choose how to expose it."""

        statement = (
            select(
                model_registry,
                provider_discovery_state.c.state.label("discovery_state"),
                provider_discovery_state.c.last_success_at.label("last_success_at"),
            )
            .select_from(
                model_registry.outerjoin(
                    provider_discovery_state,
                    model_registry.c.provider == provider_discovery_state.c.provider,
                )
            )
            .order_by(model_registry.c.canonical_id)
        )
        if provider is not None:
            statement = statement.where(model_registry.c.provider == _normalise_provider(provider))
        with self._engine.connect() as connection:
            return [_model_record(row) for row in connection.execute(statement).mappings()]

    def resolve_model(self, identifier: str) -> ModelRecord | None:
        """Resolve an exact canonical ID first, then a globally unique alias."""

        lookup = _normalise_lookup_identifier(identifier)
        canonical_lookup: str | None = None
        if "/" in lookup:
            try:
                canonical_lookup = _normalise_canonical_id(lookup)
            except ModelRegistryError:
                canonical_lookup = None

        base_statement = select(
            model_registry,
            provider_discovery_state.c.state.label("discovery_state"),
            provider_discovery_state.c.last_success_at.label("last_success_at"),
        ).select_from(
            model_registry.join(
                provider_discovery_state,
                model_registry.c.provider == provider_discovery_state.c.provider,
            )
        )
        with self._engine.connect() as connection:
            if canonical_lookup is not None:
                row = connection.execute(
                    base_statement.where(
                        model_registry.c.canonical_id == canonical_lookup
                    )
                ).mappings().first()
                if row is not None:
                    return _model_record(row)
            row = connection.execute(
                base_statement.where(model_registry.c.alias == lookup)
            ).mappings().first()
        return _model_record(row) if row is not None else None

    def discovery_state(self, provider: str) -> DiscoverySnapshot | None:
        """Return the latest discovery metadata without exposing raw errors."""

        normalized_provider = _normalise_provider(provider)
        with self._engine.connect() as connection:
            row = connection.execute(
                select(provider_discovery_state).where(
                    provider_discovery_state.c.provider == normalized_provider
                )
            ).mappings().first()
        if row is None:
            return None
        return DiscoverySnapshot(
            provider=normalized_provider,
            state=DiscoveryState(row["state"]),
            last_success_at=(
                _as_utc(row["last_success_at"])
                if row["last_success_at"] is not None
                else None
            ),
            last_attempt_at=_as_utc(row["last_attempt_at"]),
            error_category=row["error_category"],
        )

    def discovery_state_tuple(
        self, provider: str
    ) -> tuple[DiscoveryState, datetime | None, datetime, str | None] | None:
        """Compatibility view for callers that still consume the original tuple."""

        snapshot = self.discovery_state(provider)
        if snapshot is None:
            return None
        return (
            snapshot.state,
            snapshot.last_success_at,
            snapshot.last_attempt_at,
            snapshot.error_category,
        )

    @staticmethod
    def _upsert_discovery_state(
        connection: object,
        *,
        provider: str,
        state: DiscoveryState,
        last_success_at: datetime | None,
        last_attempt_at: datetime,
        error_category: str | None,
    ) -> None:
        # SQLite's conflict syntax is intentionally avoided here so this Core
        # component remains straightforward to exercise with other dialects.
        existing = connection.execute(  # type: ignore[union-attr]
            select(provider_discovery_state.c.provider).where(
                provider_discovery_state.c.provider == provider
            )
        ).first()
        values = {
            "state": state.value,
            "last_success_at": last_success_at,
            "last_attempt_at": last_attempt_at,
            "error_category": error_category,
        }
        if existing:
            connection.execute(  # type: ignore[union-attr]
                update(provider_discovery_state)
                .where(provider_discovery_state.c.provider == provider)
                .values(**values)
            )
        else:
            connection.execute(  # type: ignore[union-attr]
                insert(provider_discovery_state).values(provider=provider, **values)
            )


def _validate_models(provider: str, models: Iterable[DiscoveredModel]) -> list[DiscoveredModel]:
    validated: list[DiscoveredModel] = []
    upstream_ids: set[str] = set()
    for discovered in models:
        if not isinstance(discovered, DiscoveredModel):
            raise ModelRegistryError("discovery results must contain DiscoveredModel values")
        upstream_id = _normalise_upstream_id(discovered.upstream_id)
        if upstream_id in upstream_ids:
            raise ModelRegistryError("discovery result contains a duplicate upstream model ID")
        upstream_ids.add(upstream_id)
        display_name = _normalise_display_name(discovered.display_name)
        validated.append(DiscoveredModel(upstream_id=upstream_id, display_name=display_name))
    return validated


def _normalise_provider(provider: str) -> str:
    if not isinstance(provider, str):
        raise ModelRegistryError("provider must be a string")
    normalized = provider.strip().lower()
    if not normalized or not _PROVIDER_PATTERN.fullmatch(normalized):
        raise ModelRegistryError("provider must be a non-empty normalized identifier")
    return normalized


def _normalise_upstream_id(upstream_id: str) -> str:
    if not isinstance(upstream_id, str):
        raise ModelRegistryError("upstream model ID must be a string")
    normalized = upstream_id.strip()
    if (
        not normalized
        or len(normalized) > 512
        or _contains_control_character(normalized)
    ):
        raise ModelRegistryError("upstream model ID must be a non-empty safe string")
    return normalized


def _normalise_display_name(display_name: str | None) -> str | None:
    if display_name is None:
        return None
    if not isinstance(display_name, str):
        raise ModelRegistryError("display name must be a string or None")
    normalized = display_name.strip()
    if not normalized:
        return None
    if len(normalized) > 512 or _contains_control_character(normalized):
        raise ModelRegistryError("display name is invalid")
    return normalized


def _normalise_alias(alias: str | None) -> str | None:
    if alias is None:
        return None
    if not isinstance(alias, str):
        raise ModelRegistryError("alias must be a string or None")
    normalized = alias.strip()
    if not normalized:
        return None
    if len(normalized) > 512 or _contains_control_character(normalized):
        raise ModelRegistryError("alias is invalid")
    return normalized


def _normalise_canonical_id(canonical_id: str) -> str:
    if not isinstance(canonical_id, str) or "/" not in canonical_id:
        raise ModelRegistryError("canonical model ID must be in provider/upstream_id form")
    provider, upstream_id = canonical_id.split("/", 1)
    return canonical_model_id(provider, upstream_id)


def _normalise_lookup_identifier(identifier: str) -> str:
    if not isinstance(identifier, str):
        raise ModelRegistryError("model identifier must be a string")
    normalized = identifier.strip()
    if (
        not normalized
        or len(normalized) > 512
        or _contains_control_character(normalized)
    ):
        raise ModelRegistryError("model identifier must be a non-empty safe string")
    return normalized


def _normalise_error_category(error_category: str) -> str:
    if not isinstance(error_category, str):
        raise ModelRegistryError("error category must be a string")
    normalized = error_category.strip().lower()
    if not normalized or not _ERROR_CATEGORY_PATTERN.fullmatch(normalized):
        raise ModelRegistryError("error category must be a safe category identifier")
    return normalized


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _model_record(row: RowMapping) -> ModelRecord:
    return ModelRecord(
        canonical_id=row["canonical_id"],
        provider=row["provider"],
        upstream_id=row["upstream_id"],
        display_name=row["display_name"],
        alias=row["alias"],
        available=bool(row["available"]),
        discovered_at=_as_utc(row["discovered_at"]),
        discovery_state=DiscoveryState(row["discovery_state"]),
        last_success_at=(
            _as_utc(row["last_success_at"])
            if row["last_success_at"] is not None
            else None
        ),
    )
