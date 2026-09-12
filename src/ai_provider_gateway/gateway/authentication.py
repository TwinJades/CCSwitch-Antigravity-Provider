"""Gateway API-key persistence and verification.

This module deliberately stores only an opaque key prefix and an Argon2
verifier. It has no dependency on administrator sessions, OAuth, or Sidecar
management credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, insert, select, update
from sqlalchemy.engine import Engine

from ..security.passwords import hash_password, verify_password
from ..security.secrets import generate_secret


class GatewayKeyError(ValueError):
    """A safe API-key validation or storage error."""


@dataclass(frozen=True, slots=True)
class IssuedGatewayKey:
    """The full key is returned exactly once and is intentionally non-reprable."""

    key_id: str
    prefix: str
    secret: str = field(repr=False)
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedGatewayKey:
    """Safe identity of an active caller; it never includes the supplied key."""

    key_id: str
    prefix: str


@dataclass(frozen=True, slots=True)
class GatewayKeyMetadata:
    """Non-secret metadata safe to return from the local control plane."""

    key_id: str
    prefix: str
    active: bool
    created_at: datetime
    last_used_at: datetime | None
    deleted_at: datetime | None


_metadata = MetaData()
gateway_api_keys = Table(
    "gateway_api_keys",
    _metadata,
    Column("key_id", String(64), primary_key=True),
    Column("prefix", String(64), nullable=False, unique=True),
    Column("verifier", String(512), nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_used_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

_Hasher = Callable[[str], str]
_Verifier = Callable[[str, str], bool]
_SecretFactory = Callable[[int], str]


class GatewayApiKeyStore:
    """Manage multiple locally persisted data-plane API keys."""

    def __init__(
        self,
        engine: Engine,
        *,
        hasher: _Hasher = hash_password,
        verifier: _Verifier = verify_password,
        secret_factory: _SecretFactory = generate_secret,
    ) -> None:
        self._engine = engine
        self._hasher = hasher
        self._verifier = verifier
        self._secret_factory = secret_factory

    def create(self, key_id: str) -> IssuedGatewayKey:
        """Create one active key; return its full secret only to this caller."""

        normalized_id = _normalise_key_id(key_id)
        secret = self._secret_factory(32)
        if not isinstance(secret, str) or not 32 <= len(secret) <= 512:
            raise GatewayKeyError("secret factory returned an invalid key")
        prefix = secret[:16]
        created_at = datetime.now(UTC)
        try:
            encoded = self._hasher(secret)
        except Exception:
            raise GatewayKeyError("API-key verifier could not be created") from None
        if not isinstance(encoded, str) or not encoded:
            raise GatewayKeyError("API-key verifier could not be created")
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    insert(gateway_api_keys).values(
                        key_id=normalized_id,
                        prefix=prefix,
                        verifier=encoded,
                        active=True,
                        created_at=created_at,
                        last_used_at=None,
                        revoked_at=None,
                    )
                )
        except Exception:
            raise GatewayKeyError("API key could not be stored") from None
        return IssuedGatewayKey(normalized_id, prefix, secret, created_at)

    def authenticate(self, supplied_key: str | None) -> AuthenticatedGatewayKey | None:
        """Authenticate an active key without retaining or exposing its value."""

        if (
            not isinstance(supplied_key, str)
            or len(supplied_key) < 16
            or len(supplied_key) > 512
        ):
            return None
        prefix = supplied_key[:16]
        with self._engine.connect() as connection:
            row = connection.execute(
                select(gateway_api_keys).where(
                    gateway_api_keys.c.prefix == prefix,
                    gateway_api_keys.c.active.is_(True),
                )
            ).mappings().first()
        if row is None:
            return None
        try:
            valid = self._verifier(row["verifier"], supplied_key)
        except Exception:
            return None
        if not valid:
            return None
        used_at = datetime.now(UTC)
        with self._engine.begin() as connection:
            refreshed = connection.execute(
                update(gateway_api_keys)
                .where(
                    gateway_api_keys.c.key_id == row["key_id"],
                    gateway_api_keys.c.active.is_(True),
                )
                .values(last_used_at=used_at)
            )
        if refreshed.rowcount != 1:
            return None
        return AuthenticatedGatewayKey(row["key_id"], row["prefix"])

    def list_metadata(self) -> tuple[GatewayKeyMetadata, ...]:
        """List every key without returning a verifier or full secret."""

        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    gateway_api_keys.c.key_id,
                    gateway_api_keys.c.prefix,
                    gateway_api_keys.c.active,
                    gateway_api_keys.c.created_at,
                    gateway_api_keys.c.last_used_at,
                    gateway_api_keys.c.revoked_at,
                ).order_by(
                    gateway_api_keys.c.created_at.desc(),
                    gateway_api_keys.c.key_id,
                )
            ).mappings()
            return tuple(
                GatewayKeyMetadata(
                    key_id=row["key_id"],
                    prefix=row["prefix"],
                    active=bool(row["active"]),
                    created_at=row["created_at"],
                    last_used_at=row["last_used_at"],
                    deleted_at=row["revoked_at"],
                )
                for row in rows
            )

    def revoke(self, key_id: str) -> bool:
        """Revoke a key idempotently without deleting its audit-safe metadata."""

        normalized_id = _normalise_key_id(key_id)
        with self._engine.begin() as connection:
            result = connection.execute(
                update(gateway_api_keys)
                .where(
                    gateway_api_keys.c.key_id == normalized_id,
                    gateway_api_keys.c.active.is_(True),
                )
                .values(active=False, revoked_at=datetime.now(UTC))
            )
        return result.rowcount == 1


def _normalise_key_id(key_id: str) -> str:
    if not isinstance(key_id, str):
        raise GatewayKeyError("key ID must be a string")
    normalized = key_id.strip()
    if not normalized or len(normalized) > 64 or any(not (c.isalnum() or c in "_-.") for c in normalized):
        raise GatewayKeyError("key ID must be a safe non-empty identifier")
    return normalized
