"""Safe HTTP adapter for a user-configured OpenAI-compatible endpoint."""
from __future__ import annotations
from email.utils import parsedate_to_datetime
from typing import Any
import httpx
from .storage import OpenAICompatibleProvider, ProviderModel

class OpenAICompatibleProviderError(RuntimeError):
    """Safe error classification; it never includes provider response content."""
    def __init__(self, category: str, status_code: int | None = None, retry_after: str | None = None) -> None:
        self.category, self.status_code, self.retry_after = category, status_code, retry_after
        super().__init__("The configured provider request failed.")

class OpenAICompatibleDataPlane:
    """One request-scoped client so changed configuration applies immediately."""
    def __init__(self, provider: OpenAICompatibleProvider, *, timeout: float = 60.0, client: httpx.AsyncClient | None = None) -> None:
        headers = {item.name: item.value for item in provider.headers}
        if provider.api_key is not None:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        if client is not None:
            if str(client.base_url).rstrip("/") != provider.base_url.rstrip("/") or client.follow_redirects:
                raise ValueError("Injected provider client must use the configured URL without redirects.")
            client.headers.update(headers)
        self._client = client or httpx.AsyncClient(base_url=provider.base_url, timeout=timeout, follow_redirects=False, trust_env=False, headers=headers)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post("chat/completions", json=payload)
        except httpx.RequestError as error:
            raise _transport_error(error) from error
        self._raise_for_status(response)
        try:
            body = response.json()
        except ValueError as error:
            raise OpenAICompatibleProviderError("provider_protocol_error") from error
        if not isinstance(body, dict):
            raise OpenAICompatibleProviderError("provider_protocol_error")
        return body

    async def discover_models(self) -> tuple[ProviderModel, ...]:
        """Fetch the standard model catalogue without sending a chat request."""

        try:
            response = await self._client.get("models")
        except httpx.RequestError as error:
            raise _transport_error(error) from error
        self._raise_for_status(response)
        try:
            body = response.json()
            data = body["data"]
            if not isinstance(data, list) or not data or len(data) > 4096:
                raise ValueError
            result: list[ProviderModel] = []
            seen: set[str] = set()
            for item in data:
                model_id = item["id"] if isinstance(item, dict) else None
                if (
                    not isinstance(model_id, str)
                    or not model_id.strip()
                    or len(model_id) > 512
                    or any(ord(character) < 32 or ord(character) == 127 for character in model_id)
                    or model_id in seen
                ):
                    raise ValueError
                seen.add(model_id)
                result.append(ProviderModel(model_id))
        except (KeyError, TypeError, ValueError) as error:
            raise OpenAICompatibleProviderError("provider_protocol_error") from error
        return tuple(result)

    async def open_chat_completion_stream(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            request = self._client.build_request("POST", "chat/completions", json=payload, headers={"Accept": "text/event-stream"})
            response = await self._client.send(request, stream=True)
        except httpx.RequestError as error:
            raise _transport_error(error) from error
        try:
            if response.is_error:
                await response.aread()
            self._raise_for_status(response)
            if not response.headers.get("content-type", "").lower().startswith("text/event-stream"):
                raise OpenAICompatibleProviderError("provider_protocol_error")
        except Exception:
            await response.aclose()
            raise
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_error:
            status = response.status_code if response.status_code in {400, 401, 403, 404, 408, 429, 500, 502, 503, 504} else 502
            categories = {
                401: "provider_authentication_failed",
                403: "provider_access_denied",
                404: "provider_endpoint_not_found",
                408: "provider_timeout",
                429: "provider_rate_limited",
            }
            category = categories.get(status, "provider_upstream_error")
            raise OpenAICompatibleProviderError(category, status, _safe_retry_after(response.headers.get("retry-after")))


def _transport_error(error: httpx.RequestError) -> OpenAICompatibleProviderError:
    if isinstance(error, httpx.ConnectTimeout):
        return OpenAICompatibleProviderError("provider_connect_timeout")
    if isinstance(error, httpx.TimeoutException):
        return OpenAICompatibleProviderError("provider_timeout")
    if isinstance(error, httpx.ConnectError):
        return OpenAICompatibleProviderError("provider_connect_error")
    return OpenAICompatibleProviderError("provider_unavailable")

def _safe_retry_after(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.isascii() and normalized.isdigit() and len(normalized) <= 10:
        return normalized
    if len(normalized) > 64 or any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        return None
    try:
        parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized
