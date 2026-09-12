"""Validated, DPAPI-protected storage for configured OpenAI providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Column, DateTime, LargeBinary, MetaData, String, Table, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from ...models import DiscoveredModel, ModelRegistry, ModelRegistryError


class DataProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...
    def unprotect(self, value: bytes) -> bytes: ...


class CustomProviderError(ValueError):
    """A configuration error safe to expose as a generic control API error."""


@dataclass(frozen=True, slots=True)
class ProviderModel:
    model_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderHeader:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProvider:
    provider_id: str
    display_name: str
    base_url: str
    api_key: str | None
    models: tuple[ProviderModel, ...]
    headers: tuple[ProviderHeader, ...]

    def __repr__(self) -> str:
        return (
            "OpenAICompatibleProvider(provider_id={!r}, display_name={!r}, "
            "base_url={!r}, api_key=<redacted>, models={!r}, headers=<redacted>)"
        ).format(self.provider_id, self.display_name, self.base_url, self.models)


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    provider_id: str
    display_name: str
    base_url: str
    api_key_configured: bool
    header_names: tuple[str, ...]
    model_count: int
    verification_status: str
    verification_error: str | None


_metadata = MetaData()
openai_compatible_providers = Table(
    "openai_compatible_providers", _metadata,
    Column("provider_id", String(128), primary_key=True),
    Column("display_name", String(512), nullable=False),
    Column("base_url", String(2048), nullable=False),
    Column("api_key_protected", LargeBinary, nullable=True),
    Column("headers_protected", LargeBinary, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_BLOCKED_HEADERS = frozenset({
    "host", "content-length", "connection", "transfer-encoding", "upgrade",
    "proxy-authorization", "proxy-authenticate", "cookie", "keep-alive", "te",
    "trailer", "user-agent", "forwarded", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto",
})


class CustomProviderStore:
    """Own provider persistence and registry refresh for custom providers."""

    def __init__(self, engine: Engine, protector: DataProtector) -> None:
        self._engine = engine
        self._protector = protector
        self._registry = ModelRegistry(engine)

    def replace(self, provider: OpenAICompatibleProvider) -> ProviderMetadata:
        normalized = _validate_provider(provider)
        api_key_protected = (
            self._protect(normalized.api_key) if normalized.api_key is not None else None
        )
        headers_protected = self._protect(json.dumps(
            [{"name": item.name, "value": item.value} for item in normalized.headers],
            separators=(",", ":"), ensure_ascii=True,
        ))
        now = datetime.now(UTC)
        try:
            with self._engine.begin() as connection:
                if normalized.models:
                    self._registry.refresh_configured_in_transaction(
                        connection,
                        normalized.provider_id,
                        [DiscoveredModel(item.model_id, item.display_name) for item in normalized.models],
                    )
                exists = connection.execute(select(openai_compatible_providers.c.provider_id).where(
                    openai_compatible_providers.c.provider_id == normalized.provider_id
                )).first()
                values = {
                    "display_name": normalized.display_name,
                    "base_url": normalized.base_url,
                    "api_key_protected": api_key_protected,
                    "headers_protected": headers_protected,
                    "updated_at": now,
                }
                if exists:
                    connection.execute(update(openai_compatible_providers).where(
                        openai_compatible_providers.c.provider_id == normalized.provider_id
                    ).values(**values))
                else:
                    connection.execute(insert(openai_compatible_providers).values(
                        provider_id=normalized.provider_id, **values
                    ))
        except ModelRegistryError as error:
            raise CustomProviderError("invalid provider model catalogue") from error
        except SQLAlchemyError as error:
            raise CustomProviderError("local provider storage failed") from error
        return self.metadata(normalized.provider_id)

    def list_metadata(self) -> list[ProviderMetadata]:
        with self._engine.connect() as connection:
            rows = connection.execute(select(openai_compatible_providers).order_by(
                openai_compatible_providers.c.provider_id
            )).mappings()
            result: list[ProviderMetadata] = []
            for row in rows:
                headers = self._decode_headers(row["headers_protected"])
                count = sum(
                    item.available
                    for item in self._registry.list_models(row["provider_id"])
                )
                verification_status, verification_error = self._verification(row["provider_id"])
                result.append(ProviderMetadata(
                    provider_id=row["provider_id"], display_name=row["display_name"],
                    base_url=row["base_url"], api_key_configured=row["api_key_protected"] is not None,
                    header_names=tuple(item.name for item in headers), model_count=count,
                    verification_status=verification_status,
                    verification_error=verification_error,
                ))
            return result

    def metadata(self, provider_id: str) -> ProviderMetadata:
        normalized_id = _provider_id(provider_id)
        for item in self.list_metadata():
            if item.provider_id == normalized_id:
                return item
        raise CustomProviderError("provider not configured")

    def refresh_discovered_models(
        self, provider_id: str, models: tuple[ProviderModel, ...]
    ) -> ProviderMetadata:
        normalized_id = _provider_id(provider_id)
        if self.load(normalized_id) is None:
            raise CustomProviderError("provider not configured")
        try:
            self._registry.refresh_success(
                normalized_id,
                [DiscoveredModel(item.model_id, item.display_name) for item in _validate_models(models)],
            )
        except ModelRegistryError as error:
            raise CustomProviderError("invalid discovered model catalogue") from error
        return self.metadata(normalized_id)

    def record_verification_failure(
        self, provider_id: str, error_category: str
    ) -> ProviderMetadata:
        normalized_id = _provider_id(provider_id)
        if self.load(normalized_id) is None:
            raise CustomProviderError("provider not configured")
        try:
            self._registry.record_failure(normalized_id, error_category)
        except ModelRegistryError as error:
            raise CustomProviderError("invalid provider verification state") from error
        return self.metadata(normalized_id)

    def load(self, provider_id: str) -> OpenAICompatibleProvider | None:
        normalized_id = _provider_id(provider_id)
        with self._engine.connect() as connection:
            row = connection.execute(select(openai_compatible_providers).where(
                openai_compatible_providers.c.provider_id == normalized_id
            )).mappings().first()
        if row is None:
            return None
        api_key = self._unprotect(row["api_key_protected"]) if row["api_key_protected"] else None
        headers = self._decode_headers(row["headers_protected"])
        models = tuple(ProviderModel(item.upstream_id, item.display_name) for item in self._registry.list_models(normalized_id) if item.available)
        return OpenAICompatibleProvider(
            provider_id=normalized_id, display_name=row["display_name"], base_url=row["base_url"],
            api_key=api_key, models=models, headers=headers,
        )

    def _protect(self, value: str) -> bytes:
        try:
            return self._protector.protect(value.encode("utf-8"))
        except Exception as error:
            raise CustomProviderError("local credential protection failed") from error

    def _unprotect(self, value: bytes) -> str:
        try:
            decoded = self._protector.unprotect(value).decode("utf-8")
        except Exception as error:
            raise CustomProviderError("local credential access failed") from error
        if "\x00" in decoded:
            raise CustomProviderError("local credential state is invalid")
        return decoded

    def _decode_headers(self, value: bytes) -> tuple[ProviderHeader, ...]:
        try:
            decoded = json.loads(self._unprotect(value))
            if not isinstance(decoded, list):
                raise ValueError
            headers = tuple(ProviderHeader(item["name"], item["value"]) for item in decoded if isinstance(item, dict))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CustomProviderError("local provider state is invalid") from error
        return _validate_headers(headers)

    def _verification(self, provider_id: str) -> tuple[str, str | None]:
        snapshot = self._registry.discovery_state(provider_id)
        if snapshot is None or snapshot.error_category == "not_verified":
            return "unverified", None
        if snapshot.state.value == "available":
            return "verified", None
        return "connection_failed", snapshot.error_category


def _validate_provider(provider: OpenAICompatibleProvider) -> OpenAICompatibleProvider:
    if not isinstance(provider, OpenAICompatibleProvider):
        raise CustomProviderError("invalid provider configuration")
    provider_id = _provider_id(provider.provider_id)
    if provider_id == "antigravity":
        raise CustomProviderError("reserved provider identifier")
    display_name = _text(provider.display_name, 512, "display name")
    base_url = _base_url(provider.base_url)
    api_key = _optional_secret(provider.api_key, "API key")
    headers = _validate_headers(provider.headers)
    if api_key is not None and any(item.name.lower() == "authorization" for item in headers):
        raise CustomProviderError("ambiguous provider authorization")
    models = _validate_models(provider.models)
    return OpenAICompatibleProvider(provider_id, display_name, base_url, api_key, models, headers)


def _provider_id(value: str) -> str:
    if not isinstance(value, str):
        raise CustomProviderError("invalid provider identifier")
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 128 or not _PROVIDER_ID.fullmatch(normalized):
        raise CustomProviderError("invalid provider identifier")
    return normalized


def _base_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or _has_controls(value):
        raise CustomProviderError("invalid provider URL")
    parts = urlsplit(value.strip())
    if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
        raise CustomProviderError("invalid provider URL")
    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if scheme != "https" and not (scheme == "http" and loopback):
        raise CustomProviderError("provider URL must use HTTPS")
    if parts.path.rstrip("/") != "/v1":
        raise CustomProviderError("provider URL must end in /v1")
    try:
        port = parts.port
    except ValueError as error:
        raise CustomProviderError("invalid provider URL") from error
    netloc = hostname if port is None else f"{hostname}:{port}"
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]" if port is None else f"[{hostname}]:{port}"
    return urlunsplit((scheme, netloc, "/v1", "", ""))


def _validate_models(values: tuple[ProviderModel, ...]) -> tuple[ProviderModel, ...]:
    if not isinstance(values, tuple):
        raise CustomProviderError("invalid provider models")
    result: list[ProviderModel] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, ProviderModel):
            raise CustomProviderError("invalid provider model")
        model_id = _text(item.model_id, 512, "model")
        if model_id in seen:
            raise CustomProviderError("duplicate provider model")
        seen.add(model_id)
        name = _optional_text(item.display_name, 512, "model display name")
        result.append(ProviderModel(model_id, name))
    return tuple(result)


def _validate_headers(values: tuple[ProviderHeader, ...]) -> tuple[ProviderHeader, ...]:
    if not isinstance(values, tuple):
        raise CustomProviderError("invalid provider headers")
    result: list[ProviderHeader] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, ProviderHeader) or not isinstance(item.name, str):
            raise CustomProviderError("invalid provider header")
        name = item.name.strip()
        lowered = name.lower()
        if not _HEADER_NAME.fullmatch(name) or lowered in _BLOCKED_HEADERS or lowered in seen:
            raise CustomProviderError("unsafe provider header")
        value = _text(item.value, 8192, "header value", strip=False)
        seen.add(lowered)
        result.append(ProviderHeader(name, value))
    return tuple(result)


def _optional_secret(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _optional_text(value, 8192, label, strip=False)


def _optional_text(value: str | None, limit: int, label: str, *, strip: bool = True) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _has_controls(value) or len(value) > limit:
        raise CustomProviderError(f"invalid {label}")
    normalized = value.strip() if strip else value
    return normalized or None

def _text(value: str, limit: int, label: str, *, strip: bool = True) -> str:
    if not isinstance(value, str) or _has_controls(value) or len(value) > limit:
        raise CustomProviderError(f"invalid {label}")
    normalized = value.strip() if strip else value
    if not normalized:
        raise CustomProviderError(f"invalid {label}")
    return normalized


def _has_controls(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
