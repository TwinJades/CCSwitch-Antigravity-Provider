"""Narrow, local-only client for CLIProxyAPI's documented control endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import ipaddress
from typing import Any
from urllib.parse import urlparse

import httpx


class SidecarClientError(RuntimeError):
    """Base error for a CLIProxyAPI call without sensitive request details."""


class SidecarConfigurationError(SidecarClientError):
    """The local sidecar client was configured with an unsafe value."""


class SidecarTransportError(SidecarClientError):
    """The local sidecar could not be reached or returned a server error."""


class SidecarAuthenticationError(SidecarClientError):
    """CLIProxyAPI rejected the API or management credential."""


class SidecarProtocolError(SidecarClientError):
    """CLIProxyAPI returned a response outside this adapter's narrow contract."""


@dataclass(frozen=True, slots=True)
class SidecarApiKey:
    """Credential for the Sidecar data-plane API only."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise SidecarConfigurationError("A Sidecar API key is required.")


@dataclass(frozen=True, slots=True)
class ManagementKey:
    """Credential for the Sidecar management API only."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.value.strip():
            raise SidecarConfigurationError("A management key is required.")


class OAuthAuthorizationState(StrEnum):
    """Safe, normalized OAuth state values exposed by the adapter."""

    UNKNOWN = "unknown"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AuthFileState(StrEnum):
    """Aggregate authentication-file states, without exposing file metadata."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    """A model identifier currently exposed by the authenticated Sidecar."""

    model_id: str


@dataclass(frozen=True, slots=True)
class ModelDiscovery:
    """Result of a Sidecar model discovery request."""

    models: tuple[DiscoveredModel, ...]


@dataclass(frozen=True, slots=True)
class OAuthAuthorization:
    """A verified HTTPS authorization URL; it is never logged by this module."""

    url: str = field(repr=False)
    state: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or not self.state.strip() or len(self.state) > 512:
            raise SidecarProtocolError("The OAuth response contained an invalid state.")


@dataclass(frozen=True, slots=True)
class OAuthStatus:
    """Normalized management API OAuth status without raw response contents."""

    state: OAuthAuthorizationState


@dataclass(frozen=True, slots=True)
class AuthFileSummary:
    """Counts only; intentionally excludes auth file names, paths, and identities."""

    total: int
    active: int
    inactive: int
    unknown: int


def _validate_loopback_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SidecarConfigurationError("The Sidecar base URL must be an absolute HTTP URL.")
    hostname = parsed.hostname.lower()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise SidecarConfigurationError("The Sidecar base URL must use loopback.")
        except ValueError as error:
            raise SidecarConfigurationError("The Sidecar base URL must use loopback.") from error
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SidecarConfigurationError("The Sidecar base URL cannot contain credentials or a query.")
    return base_url.rstrip("/")


def _authorization_header(key: SidecarApiKey | ManagementKey) -> dict[str, str]:
    return {"Authorization": f"Bearer {key.value}"}


class CLIProxyAPIClient:
    """Use separate data-plane and management credentials against local CLIProxyAPI."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SidecarApiKey,
        management_key: ManagementKey,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = _validate_loopback_base_url(base_url)
        self._api_key = api_key
        self._management_key = management_key
        if client is not None:
            injected_base_url = _validate_loopback_base_url(str(client.base_url))
            if injected_base_url != self._base_url or client.follow_redirects:
                raise SidecarConfigurationError(
                    "The injected HTTP client must use the same loopback URL without redirects."
                )
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> "CLIProxyAPIClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close only the HTTP client constructed by this adapter."""

        if self._owns_client:
            await self._client.aclose()

    async def discover_models(self) -> ModelDiscovery:
        """Discover dynamic model IDs through the authenticated data-plane endpoint."""

        payload = await self._get_json("/v1/models", self._api_key)
        records = payload.get("data")
        if not isinstance(records, list):
            raise SidecarProtocolError("The model response does not contain a data list.")
        model_ids: list[str] = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise SidecarProtocolError("The model response contains an invalid record.")
            model_id = record["id"].strip()
            if not model_id:
                raise SidecarProtocolError("The model response contains an empty model ID.")
            model_ids.append(model_id)
        return ModelDiscovery(models=tuple(DiscoveredModel(model_id=value) for value in model_ids))

    async def get_antigravity_auth_url(self) -> OAuthAuthorization:
        """Request, but never open, the HTTPS URL for user-mediated OAuth."""

        payload = await self._get_json(
            "/v0/management/antigravity-auth-url",
            self._management_key,
            params={"is_webui": "true"},
        )
        url = payload.get("url") or payload.get("auth_url")
        state = payload.get("state")
        parsed = urlparse(url) if isinstance(url, str) else None
        if (
            parsed is None
            or parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise SidecarProtocolError("The OAuth response did not contain an HTTPS authorization URL.")
        if not isinstance(state, str):
            raise SidecarProtocolError("The OAuth response did not contain an OAuth state.")
        return OAuthAuthorization(url=url, state=state)

    async def get_auth_status(self, state: str) -> OAuthStatus:
        """Poll a state supplied by the management API without logging it."""

        if not isinstance(state, str) or not state.strip() or len(state) > 512:
            raise SidecarConfigurationError("An OAuth state is required.")
        payload = await self._get_json(
            "/v0/management/get-auth-status",
            self._management_key,
            params={"state": state},
        )
        raw = payload.get("status", payload.get("state", ""))
        normalized = str(raw).strip().lower()
        if normalized in {"ok", "success", "succeeded", "completed", "authorized"}:
            result = OAuthAuthorizationState.SUCCEEDED
        elif normalized in {"pending", "wait", "waiting", "in_progress", "running"}:
            result = OAuthAuthorizationState.PENDING
        elif normalized in {"failed", "error", "denied", "cancelled", "canceled"}:
            result = OAuthAuthorizationState.FAILED
        else:
            result = OAuthAuthorizationState.UNKNOWN
        return OAuthStatus(state=result)

    async def get_auth_file_summary(self) -> AuthFileSummary:
        """Return auth-file health counts without returning filenames or account identities."""

        payload = await self._get_json("/v0/management/auth-files", self._management_key)
        records = _find_auth_records(payload)
        if records is None:
            raise SidecarProtocolError("The auth-file response does not contain a file list.")
        states = tuple(_auth_file_state(record) for record in records)
        active = states.count(AuthFileState.ACTIVE)
        inactive = states.count(AuthFileState.INACTIVE)
        unknown = states.count(AuthFileState.UNKNOWN)
        return AuthFileSummary(total=len(states), active=active, inactive=inactive, unknown=unknown)

    async def _get_json(
        self,
        path: str,
        key: SidecarApiKey | ManagementKey,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(path, headers=_authorization_header(key), params=params)
        except httpx.RequestError as error:
            raise SidecarTransportError("The local Sidecar could not be reached.") from error
        if response.status_code in {401, 403}:
            raise SidecarAuthenticationError("The Sidecar rejected its credential.")
        if response.is_error:
            raise SidecarTransportError("The Sidecar returned an unsuccessful response.")
        try:
            payload = response.json()
        except ValueError as error:
            raise SidecarProtocolError("The Sidecar response was not JSON.") from error
        if not isinstance(payload, dict):
            raise SidecarProtocolError("The Sidecar response must be a JSON object.")
        return payload


def _find_auth_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Find the response's top-level auth record list without returning record data."""

    for key in ("files", "auth_files", "authFiles", "data", "items"):
        candidate = payload.get(key)
        if isinstance(candidate, list):
            if any(not isinstance(record, dict) for record in candidate):
                raise SidecarProtocolError(
                    "The auth-file response contains an invalid record."
                )
            return [
                record
                for record in candidate
                if str(record.get("provider", "")).strip().lower() == "antigravity"
            ]
    return None


def _auth_file_state(record: dict[str, Any]) -> AuthFileState:
    if record.get("disabled") is True or record.get("unavailable") is True:
        return AuthFileState.INACTIVE
    raw = record.get("status", record.get("state", ""))
    value = str(raw).strip().lower()
    if value in {"active", "ok", "valid", "ready", "available"}:
        return AuthFileState.ACTIVE
    if value in {"inactive", "invalid", "expired", "error", "disabled", "failed"}:
        return AuthFileState.INACTIVE
    return AuthFileState.UNKNOWN
