"""Narrow, loopback-only CLIProxyAPI data-plane client for the Gateway."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .client import (
    DiscoveredModel,
    ModelDiscovery,
    SidecarApiKey,
    SidecarAuthenticationError,
    SidecarConfigurationError,
    SidecarProtocolError,
    SidecarTransportError,
    _authorization_header,
    _validate_loopback_base_url,
)


class SidecarUpstreamError(SidecarTransportError):
    """Safe classification of an unsuccessful upstream data-plane response."""

    def __init__(
        self,
        status_code: int,
        retry_after: str | None = None,
        *,
        error_type: str | None = None,
        param: str | None = None,
        code: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        self.error_type = error_type
        self.param = param
        self.code = code
        super().__init__("The local Sidecar returned an unsuccessful response.")


class CLIProxyAPIDataPlaneClient:
    """Forward OpenAI-compatible payloads using only the Sidecar API key."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SidecarApiKey,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = _validate_loopback_base_url(base_url)
        self._api_key = api_key
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

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_models(self) -> dict[str, Any]:
        return await self._get_json("/v1/models")

    async def discover_models(self) -> ModelDiscovery:
        """Return the same strict discovery contract used by Phase 2."""

        payload = await self.list_models()
        records = payload.get("data")
        if not isinstance(records, list):
            raise SidecarProtocolError(
                "The model response does not contain a data list."
            )
        model_ids: list[str] = []
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str):
                raise SidecarProtocolError(
                    "The model response contains an invalid record."
                )
            model_id = record["id"].strip()
            if not model_id:
                raise SidecarProtocolError(
                    "The model response contains an empty model ID."
                )
            model_ids.append(model_id)
        return ModelDiscovery(
            models=tuple(DiscoveredModel(model_id=value) for value in model_ids)
        )

    async def chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/v1/chat/completions", payload)

    async def open_chat_completion_stream(self, payload: dict[str, Any]) -> httpx.Response:
        """Open an SSE response; caller must close it after iteration or cancellation."""
        try:
            request = self._client.build_request(
                "POST",
                "/v1/chat/completions",
                headers={**_authorization_header(self._api_key), "Accept": "text/event-stream"},
                json=payload,
            )
            response = await self._client.send(request, stream=True)
        except httpx.RequestError as error:
            raise SidecarTransportError("The local Sidecar could not be reached.") from error
        try:
            if response.is_error:
                await response.aread()
            self._raise_for_status(response)
            if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                raise SidecarProtocolError("The Sidecar stream did not use server-sent events.")
        except Exception:
            await response.aclose()
            raise
        return response

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            response = await self._client.get(path, headers=_authorization_header(self._api_key))
        except httpx.RequestError as error:
            raise SidecarTransportError("The local Sidecar could not be reached.") from error
        self._raise_for_status(response)
        return _json_object(response)

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                path, headers=_authorization_header(self._api_key), json=payload
            )
        except httpx.RequestError as error:
            raise SidecarTransportError("The local Sidecar could not be reached.") from error
        self._raise_for_status(response)
        return _json_object(response)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise SidecarAuthenticationError("The Sidecar rejected its credential.")
        if response.is_error:
            retry_after = _safe_retry_after(response.headers.get("retry-after"))
            error_type, param, code = _safe_error_metadata(response)
            raise SidecarUpstreamError(
                response.status_code,
                retry_after,
                error_type=error_type,
                param=param,
                code=code,
            )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise SidecarProtocolError("The Sidecar response was not JSON.") from error
    if not isinstance(payload, dict):
        raise SidecarProtocolError("The Sidecar response must be a JSON object.")
    return payload


def _safe_error_metadata(
    response: httpx.Response,
) -> tuple[str | None, str | None, str | None]:
    """Keep only short printable OpenAI error classifiers, never raw messages."""

    try:
        payload = response.json()
    except ValueError:
        return None, None, None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
        return None, None, None
    error = payload["error"]
    return (
        _allowlisted_error_type(error.get("type")),
        _allowlisted_error_param(error.get("param")),
        _allowlisted_error_code(error.get("code")),
    )


_ALLOWED_ERROR_TYPES = frozenset(
    {
        "authentication_error",
        "invalid_request_error",
        "permission_error",
        "rate_limit_error",
        "server_error",
    }
)
_ALLOWED_ERROR_CODES = frozenset(
    {
        "context_length_exceeded",
        "insufficient_quota",
        "invalid_api_key",
        "model_not_found",
        "provider_error",
        "quota_exhausted",
        "rate_limit_exceeded",
        "server_error",
        "service_unavailable",
    }
)
_ALLOWED_ERROR_PARAMS = frozenset(
    {
        "frequency_penalty",
        "logprobs",
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "model",
        "n",
        "parallel_tool_calls",
        "presence_penalty",
        "response_format",
        "seed",
        "stop",
        "stream",
        "temperature",
        "tool_choice",
        "tools",
        "top_logprobs",
        "top_p",
        "user",
    }
)


def _allowlisted_error_type(value: object) -> str | None:
    return value if isinstance(value, str) and value in _ALLOWED_ERROR_TYPES else None


def _allowlisted_error_code(value: object) -> str | None:
    return value if isinstance(value, str) and value in _ALLOWED_ERROR_CODES else None


def _allowlisted_error_param(value: object) -> str | None:
    return value if isinstance(value, str) and value in _ALLOWED_ERROR_PARAMS else None


def _safe_retry_after(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.isascii() and normalized.isdigit() and len(normalized) <= 10:
        return normalized
    if len(normalized) > 64 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        return None
    try:
        parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized
