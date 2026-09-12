"""OpenAI-compatible Phase 3 Gateway application boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..models import ModelRecord, ModelRegistryError
from ..providers.openai_compatible import (
    CustomProviderError, CustomProviderStore, OpenAICompatibleDataPlane,
    OpenAICompatibleProviderError,
)
from ..sidecars.cliproxy.client import (
    SidecarAuthenticationError,
    SidecarProtocolError,
    SidecarTransportError,
)
from ..sidecars.cliproxy.data_plane import CLIProxyAPIDataPlaneClient, SidecarUpstreamError
from ..usage import TokenSource, UsageRecord, UsageRecorder, UsageStatus
from .authentication import AuthenticatedGatewayKey


class ModelResolver(Protocol):
    def list_models(self) -> list[ModelRecord]: ...

    def resolve_model(self, identifier: str) -> ModelRecord | None: ...


GatewayAuthorizer = Callable[[str | None], AuthenticatedGatewayKey | None]
RefreshHook = Callable[[], Awaitable[object]]
UsageValues = tuple[int | None, int | None, int | None, TokenSource]
UsageFinalize = Callable[[UsageStatus, UsageValues, str | None], None]


class GatewayService:
    """Compose only the dependencies needed by the Gateway data plane."""

    def __init__(
        self,
        *,
        registry: ModelResolver,
        data_plane: CLIProxyAPIDataPlaneClient,
        authorize: GatewayAuthorizer,
        refresh_models: RefreshHook | None = None,
        usage_recorder: UsageRecorder | None = None,
        custom_providers: CustomProviderStore | None = None,
    ) -> None:
        self.registry = registry
        self.data_plane = data_plane
        self.authorize = authorize
        self.refresh_models = refresh_models
        self.usage_recorder = usage_recorder
        self.custom_providers = custom_providers

    async def aclose(self) -> None:
        await self.data_plane.aclose()


def create_app(service: GatewayService | None = None) -> FastAPI:
    """Create a Gateway app; v1 routes exist only with complete dependencies."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if service is not None:
                await service.aclose()

    application = FastAPI(
        title="Personal AI Provider Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        phase = "phase-1"
        if service is not None:
            phase = "phase-4" if service.usage_recorder is not None else "phase-3"
        return {"service": "gateway", "status": "ok", "phase": phase}

    if service is None:
        return application

    @application.get("/v1/models", response_model=None)
    async def list_models(request: Request) -> JSONResponse:
        _, unauthorized = _authorization_result(request, service)
        if unauthorized is not None:
            return unauthorized
        return JSONResponse(
            {
                "object": "list",
                "data": [_model_object(record) for record in service.registry.list_models()],
            }
        )

    @application.post("/v1/chat/completions", response_model=None)
    async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
        caller, unauthorized = _authorization_result(request, service)
        if unauthorized is not None:
            return unauthorized
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        request_id = uuid4().hex

        def finish_usage(
            status: UsageStatus,
            usage: UsageValues = (None, None, None, TokenSource.UNKNOWN),
            error_category: str | None = None,
            *,
            provider: str = "unknown",
            canonical_model: str = "unknown",
            stream: bool = False,
        ) -> None:
            _record_usage(
                service,
                caller,
                request_id=request_id,
                started_at=started_at,
                started_clock=started_clock,
                provider=provider,
                canonical_model=canonical_model,
                stream=stream,
                status=status,
                usage=usage,
                error_category=error_category,
            )

        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            finish_usage(UsageStatus.FAILED, error_category="invalid_request")
            return _error(400, "Request body must be a JSON object.", "invalid_request_error")
        if not isinstance(payload, dict):
            finish_usage(UsageStatus.FAILED, error_category="invalid_request")
            return _error(400, "Request body must be a JSON object.", "invalid_request_error")
        is_stream = payload.get("stream") is True
        model_identifier = payload.get("model")
        if not isinstance(model_identifier, str) or not model_identifier.strip():
            finish_usage(
                UsageStatus.FAILED,
                error_category="invalid_request",
                stream=is_stream,
            )
            return _error(400, "A model is required.", "invalid_request_error", "model")
        record = await _resolve_available_model(service, model_identifier)
        if record is None:
            finish_usage(
                UsageStatus.FAILED,
                error_category="model_not_found",
                stream=is_stream,
            )
            return _error(404, "The requested model does not exist or is unavailable.", "invalid_request_error", "model", "model_not_found")
        custom_plane: OpenAICompatibleDataPlane | None = None
        if record.provider != "antigravity":
            if service.custom_providers is None:
                finish_usage(UsageStatus.FAILED, error_category="provider_unavailable", provider=record.provider, canonical_model=record.canonical_id, stream=is_stream)
                return _error(503, "The requested provider is unavailable.", "server_error", "model", "provider_unavailable")
            try:
                configured = await asyncio.to_thread(service.custom_providers.load, record.provider)
                if configured is None:
                    raise CustomProviderError("provider not configured")
                custom_plane = OpenAICompatibleDataPlane(configured)
            except CustomProviderError:
                finish_usage(UsageStatus.FAILED, error_category="provider_unavailable", provider=record.provider, canonical_model=record.canonical_id, stream=is_stream)
                return _error(503, "The requested provider is unavailable.", "server_error", "model", "provider_unavailable")
        upstream_payload = dict(payload)
        upstream_payload["model"] = record.upstream_id
        if is_stream:
            def finish_stream(
                status: UsageStatus,
                usage: UsageValues,
                error_category: str | None,
            ) -> None:
                finish_usage(
                    status,
                    usage,
                    error_category,
                    provider=record.provider,
                    canonical_model=record.canonical_id,
                    stream=True,
                )

            if custom_plane is not None:
                return await _custom_stream_response(
                    custom_plane, upstream_payload, record.canonical_id, finish_stream
                )
            return await _stream_response(
                service,
                upstream_payload,
                record.canonical_id,
                finish_stream,
            )
        try:
            response = await (custom_plane.chat_completion(upstream_payload) if custom_plane is not None else service.data_plane.chat_completion(upstream_payload))
        except OpenAICompatibleProviderError as error:
            if custom_plane is not None:
                await custom_plane.aclose()
            finish_usage(UsageStatus.FAILED, error_category=error.category, provider=record.provider, canonical_model=record.canonical_id)
            return _custom_provider_error(error)
        except SidecarUpstreamError as error:
            if error.status_code == 404:
                await _refresh_after_upstream_model_miss(service)
                finish_usage(
                    UsageStatus.FAILED,
                    error_category="model_not_found",
                    provider=record.provider,
                    canonical_model=record.canonical_id,
                )
                return _model_not_found()
            finish_usage(
                UsageStatus.FAILED,
                error_category=_usage_error_category(error),
                provider=record.provider,
                canonical_model=record.canonical_id,
            )
            return _sidecar_error(error)
        except (SidecarAuthenticationError, SidecarTransportError, SidecarProtocolError) as error:
            finish_usage(
                UsageStatus.FAILED,
                error_category=_usage_error_category(error),
                provider=record.provider,
                canonical_model=record.canonical_id,
            )
            return _sidecar_error(error)
        if custom_plane is not None:
            await custom_plane.aclose()
        response["model"] = record.canonical_id
        finish_usage(
            UsageStatus.SUCCEEDED,
            _extract_usage(response.get("usage")),
            provider=record.provider,
            canonical_model=record.canonical_id,
        )
        return JSONResponse(response)

    return application


def _authorization_result(
    request: Request, service: GatewayService
) -> tuple[AuthenticatedGatewayKey | Any | None, JSONResponse | None]:
    authorization = request.headers.get("authorization")
    token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
    caller = service.authorize(token)
    if caller is None:
        return None, _error(
            401,
            "Invalid API key.",
            "authentication_error",
            code="invalid_api_key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return caller, None


async def _resolve_available_model(service: GatewayService, identifier: str) -> ModelRecord | None:
    try:
        record = service.registry.resolve_model(identifier)
    except ModelRegistryError:
        return None
    if record is not None and record.available:
        return record
    if service.refresh_models is not None:
        try:
            await service.refresh_models()
        except Exception:
            # A failed dynamic refresh intentionally leaves the previous stale
            # catalogue usable, while an unavailable record remains rejected.
            pass
        try:
            record = service.registry.resolve_model(identifier)
        except ModelRegistryError:
            return None
    return record if record is not None and record.available else None


async def _stream_response(
    service: GatewayService,
    payload: dict[str, Any],
    canonical_model: str,
    finish_usage: UsageFinalize | None = None,
) -> JSONResponse | StreamingResponse:
    if finish_usage is None:
        finish_usage = lambda _status, _usage, _category: None
    try:
        upstream = await service.data_plane.open_chat_completion_stream(payload)
    except SidecarUpstreamError as error:
        if error.status_code == 404:
            await _refresh_after_upstream_model_miss(service)
            finish_usage(
                UsageStatus.FAILED,
                (None, None, None, TokenSource.UNKNOWN),
                "model_not_found",
            )
            return _model_not_found()
        finish_usage(
            UsageStatus.FAILED,
            (None, None, None, TokenSource.UNKNOWN),
            _usage_error_category(error),
        )
        return _sidecar_error(error)
    except (SidecarAuthenticationError, SidecarTransportError, SidecarProtocolError) as error:
        finish_usage(
            UsageStatus.FAILED,
            (None, None, None, TokenSource.UNKNOWN),
            _usage_error_category(error),
        )
        return _sidecar_error(error)

    return _ClosingStreamingResponse(
        _ClosingSSEStream(upstream, canonical_model, finish_usage),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _custom_stream_response(
    data_plane: OpenAICompatibleDataPlane,
    payload: dict[str, Any],
    canonical_model: str,
    finish_usage: UsageFinalize,
) -> JSONResponse | StreamingResponse:
    try:
        upstream = await data_plane.open_chat_completion_stream(payload)
    except OpenAICompatibleProviderError as error:
        await data_plane.aclose()
        finish_usage(UsageStatus.FAILED, (None, None, None, TokenSource.UNKNOWN), error.category)
        return _custom_provider_error(error)

    async def iterator() -> AsyncIterator[bytes]:
        stream = _ClosingSSEStream(upstream, canonical_model, finish_usage)
        try:
            async for line in stream:
                yield line
        finally:
            await stream.aclose()
            await data_plane.aclose()

    return _ClosingStreamingResponse(
        iterator(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

class _ClosingStreamingResponse(StreamingResponse):
    """Always close an attached upstream stream when the ASGI response ends."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close_body = getattr(self.body_iterator, "aclose", None)
            if close_body is not None:
                await close_body()


class _ClosingSSEStream:
    """Close the upstream even if downstream cancels before the first event."""

    def __init__(
        self,
        upstream: httpx.Response,
        canonical_model: str,
        finish_usage: UsageFinalize | None = None,
    ) -> None:
        self._upstream = upstream
        self._canonical_model = canonical_model
        self._lines = upstream.aiter_lines().__aiter__()
        self._closed = False
        self._finish_usage = finish_usage
        self._usage: UsageValues = (None, None, None, TokenSource.UNKNOWN)
        self._saw_done = False
        self._ended = False
        self._failed = False
        self._finalized = False

    def __aiter__(self) -> "_ClosingSSEStream":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            line = await self._lines.__anext__()
        except StopAsyncIteration:
            self._ended = True
            await self.aclose()
            raise
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except BaseException:
            self._failed = True
            await self.aclose()
            raise
        usage = _extract_sse_usage(line)
        if usage is not None:
            self._usage = usage
        if line.startswith("data:") and line[5:].strip() == "[DONE]":
            self._saw_done = True
        return _rewrite_sse_line(line, self._canonical_model)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_lines = getattr(self._lines, "aclose", None)
        try:
            if close_lines is not None:
                await close_lines()
        finally:
            try:
                await self._upstream.aclose()
            finally:
                self._finish_once()

    def _finish_once(self) -> None:
        if self._finalized or self._finish_usage is None:
            return
        self._finalized = True
        if self._saw_done:
            status, category = UsageStatus.SUCCEEDED, None
        elif self._failed or self._ended:
            status, category = UsageStatus.FAILED, "stream_incomplete"
        else:
            status, category = UsageStatus.ABORTED, "client_disconnected"
        try:
            self._finish_usage(status, self._usage, category)
        except Exception:
            # Accounting must never prevent the upstream stream from closing.
            pass


def _rewrite_sse_line(line: str, canonical_model: str) -> bytes:
    """Rewrite only a top-level SSE chunk model; preserve tool deltas verbatim."""

    if not line.startswith("data:"):
        return (line + "\n").encode("utf-8")
    value = line[5:].lstrip()
    if value == "[DONE]":
        return b"data: [DONE]\n"
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return (line + "\n").encode("utf-8")
    if not isinstance(payload, dict):
        return (line + "\n").encode("utf-8")
    payload["model"] = canonical_model
    return ("data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _extract_sse_usage(line: str) -> UsageValues | None:
    if not line.startswith("data:"):
        return None
    value = line[5:].lstrip()
    if value == "[DONE]":
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return None
    usage = _extract_usage(payload["usage"])
    return usage if usage[3] is TokenSource.UPSTREAM_REPORTED else None


def _extract_usage(value: object) -> UsageValues:
    if not isinstance(value, dict):
        return None, None, None, TokenSource.UNKNOWN

    def token(*names: str) -> int | None:
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                return candidate
        return None

    input_tokens = token("prompt_tokens", "input_tokens")
    output_tokens = token("completion_tokens", "output_tokens")
    total_tokens = token("total_tokens")
    source = (
        TokenSource.UPSTREAM_REPORTED
        if any(item is not None for item in (input_tokens, output_tokens, total_tokens))
        else TokenSource.UNKNOWN
    )
    return input_tokens, output_tokens, total_tokens, source


def _record_usage(
    service: GatewayService,
    caller: object,
    *,
    request_id: str,
    started_at: datetime,
    started_clock: float,
    provider: str,
    canonical_model: str,
    stream: bool,
    status: UsageStatus,
    usage: UsageValues,
    error_category: str | None,
) -> None:
    if service.usage_recorder is None:
        return
    client_key_id = getattr(caller, "key_id", "unknown")
    if not isinstance(client_key_id, str):
        client_key_id = "unknown"
    input_tokens, output_tokens, total_tokens, token_source = usage
    try:
        service.usage_recorder.record(
            UsageRecord(
                request_id=request_id,
                timestamp=started_at,
                client_key_id=client_key_id,
                provider=provider,
                canonical_model=canonical_model,
                stream=stream,
                status=status,
                latency_ms=max(0, round((monotonic() - started_clock) * 1000)),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                token_source=token_source,
                error_category=error_category,
            )
        )
    except Exception:
        # Local accounting failure must not change inference semantics.
        pass


def _usage_error_category(error: Exception) -> str:
    if isinstance(error, SidecarAuthenticationError):
        return "sidecar_auth_failed"
    if isinstance(error, SidecarProtocolError):
        return "sidecar_protocol_error"
    if isinstance(error, SidecarUpstreamError):
        if error.code:
            return error.code
        if error.status_code == 429:
            return "rate_limit_exceeded"
        return "provider_error"
    if isinstance(error, SidecarTransportError):
        return "sidecar_unavailable"
    return "provider_error"


def _model_object(record: ModelRecord) -> dict[str, Any]:
    return {
        "id": record.canonical_id,
        "object": "model",
        "created": int(record.discovered_at.timestamp()),
        "owned_by": record.provider,
        "display_name": record.display_name,
        "alias": record.alias,
        "available": record.available,
        "upstream_model": record.upstream_id,
        "discovered_at": record.discovered_at.isoformat(),
        "last_success_at": record.last_success_at.isoformat() if record.last_success_at else None,
        "discovery_state": record.discovery_state.value,
    }


def _sidecar_error(error: Exception) -> JSONResponse:
    if isinstance(error, SidecarAuthenticationError):
        return _error(502, "The local provider rejected its internal credential.", "server_error", code="sidecar_auth_failed")
    if isinstance(error, SidecarUpstreamError):
        status = error.status_code if error.status_code in {400, 404, 429, 500, 503} else 502
        error_type = error.error_type or (
            "invalid_request_error" if status in {400, 404} else "server_error"
        )
        headers = {"Retry-After": error.retry_after} if error.retry_after else None
        return _error(
            status,
            "The provider request failed.",
            error_type,
            param=error.param,
            code=error.code or "provider_error",
            headers=headers,
        )
    if isinstance(error, SidecarProtocolError):
        return _error(502, "The provider returned an invalid response.", "server_error", code="sidecar_protocol_error")
    return _error(503, "The local provider is unavailable.", "server_error", code="sidecar_unavailable")


async def _refresh_after_upstream_model_miss(service: GatewayService) -> None:
    if service.refresh_models is None:
        return
    try:
        await service.refresh_models()
    except Exception:
        pass


def _custom_provider_error(error: OpenAICompatibleProviderError) -> JSONResponse:
    status = error.status_code if error.status_code in {400, 401, 403, 404, 408, 429, 500, 502, 503, 504} else 503
    if error.category == "provider_timeout":
        status = 504
    headers = {"Retry-After": error.retry_after} if error.retry_after else None
    error_type = "invalid_request_error" if status in {400, 401, 403, 404, 408} else "server_error"
    return _error(status, "The provider request failed.", error_type, code=error.category, headers=headers)

def _model_not_found() -> JSONResponse:
    return _error(
        404,
        "The requested model does not exist or is unavailable.",
        "invalid_request_error",
        "model",
        "model_not_found",
    )


def _error(
    status: int,
    message: str,
    error_type: str,
    param: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": error_type, "param": param, "code": code}},
        headers=headers,
    )


app = create_app()
