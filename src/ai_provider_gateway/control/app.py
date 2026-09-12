"""Local Supervisor HTTP application.

Network binding is intentionally left to the process launcher.  This module
constructs the control-plane ASGI application without import-time process or
filesystem work. Phase 2 routes require an explicitly injected authorizer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
import inspect
from typing import Protocol

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, SecretStr

from ..models import ModelRegistryError
from ..portable import PortableConfigurationError
from ..providers.openai_compatible import (
    CustomProviderError,
    CustomProviderStore,
    OpenAICompatibleDataPlane,
    OpenAICompatibleProvider,
    OpenAICompatibleProviderError,
    ProviderHeader,
    ProviderModel,
)
from ..gateway.authentication import GatewayApiKeyStore, GatewayKeyError
from ..sidecars.cliproxy.client import (
    SidecarAuthenticationError,
    SidecarConfigurationError,
    SidecarProtocolError,
    SidecarTransportError,
)
from .auth import AdminSessionManager
from .dashboard import DASHBOARD_HTML
from .password_recovery import PasswordRecoveryManager
from .phase2 import Phase2Control
from .phase4 import Phase4Control
from .phase5 import Phase5Control
from .shutdown import ShutdownRequest
from ..integrations import IntegrationConfigError
from ..sidecars.cliproxy.updater import SidecarUpdateError, SidecarUpdateManager
from ..usage import UsageQuery


ControlAuthorizer = Callable[[Request], bool | Awaitable[bool]]


class AntigravityProxyController(Protocol):
    def configured(self) -> bool: ...
    async def replace(self, value: str | None) -> tuple[bool, object]: ...


class OAuthStatusRequest(BaseModel):
    oauth_state: SecretStr


class AdminLoginRequest(BaseModel):
    password: SecretStr


class PasswordRecoveryCompleteRequest(BaseModel):
    recovery_token: SecretStr
    new_password: SecretStr


class SidecarInstallRequest(BaseModel):
    candidate_id: str


class GatewayKeyCreateRequest(BaseModel):
    label: str


class ExportProviderSelectionRequest(BaseModel):
    provider_ids: list[str] = Field(default_factory=list)


class CustomProviderModelRequest(BaseModel):
    model_id: str
    display_name: str | None = None


class CustomProviderHeaderRequest(BaseModel):
    name: str
    value: SecretStr


class CustomProviderRequest(BaseModel):
    provider_id: str
    display_name: str
    base_url: str
    api_key: SecretStr | None = None
    models: list[CustomProviderModelRequest] = Field(default_factory=list)
    headers: list[CustomProviderHeaderRequest] = Field(default_factory=list)


class AntigravityProxyRequest(BaseModel):
    proxy_url: str | None = Field(default=None, max_length=2048)


def create_app(
    *,
    phase2: Phase2Control | None = None,
    phase4: Phase4Control | None = None,
    phase5: Phase5Control | None = None,
    authorize_control: ControlAuthorizer | None = None,
    admin_sessions: AdminSessionManager | None = None,
    password_recovery: PasswordRecoveryManager | None = None,
    sidecar_updates: SidecarUpdateManager | None = None,
    shutdown_request: ShutdownRequest | None = None,
    gateway_keys: GatewayApiKeyStore | None = None,
    custom_providers: CustomProviderStore | None = None,
    custom_provider_probe_factory: Callable[[OpenAICompatibleProvider], object] | None = None,
    antigravity_proxy: AntigravityProxyController | None = None,
    dashboard_password_bypass: bool = False,
    auto_start_sidecar: bool = False,
) -> FastAPI:
    """Create the Supervisor, refusing to mount control routes without auth."""

    if (phase2 is None) != (authorize_control is None):
        raise ValueError("Phase 2 control and its authorizer must be configured together")
    if phase4 is not None and phase2 is None:
        raise ValueError("Phase 4 statistics require the authenticated control plane")
    if phase5 is not None and phase2 is None:
        raise ValueError("Phase 5 integrations require the authenticated control plane")
    if password_recovery is not None and admin_sessions is None:
        raise ValueError("Password recovery requires administrator sessions")
    if sidecar_updates is not None and phase2 is None:
        raise ValueError("Sidecar updates require the authenticated control plane")
    if shutdown_request is not None and phase2 is None:
        raise ValueError("Application shutdown requires the authenticated control plane")
    if gateway_keys is not None and phase2 is None:
        raise ValueError("Gateway key management requires the authenticated control plane")
    if custom_providers is not None and phase2 is None:
        raise ValueError("Custom provider management requires the authenticated control plane")
    if antigravity_proxy is not None and phase2 is None:
        raise ValueError("Antigravity proxy management requires the authenticated control plane")
    if dashboard_password_bypass and (phase2 is None or authorize_control is None):
        raise ValueError("Dashboard password bypass requires the local control plane")
    if dashboard_password_bypass and password_recovery is not None:
        raise ValueError("Password recovery cannot be active while password login is bypassed")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        statistics_stop = asyncio.Event()
        if auto_start_sidecar and phase2 is not None:
            if sidecar_updates is not None:
                await sidecar_updates.recover_interrupted_update()
            await phase2.start_sidecar()
        statistics_task = (
            asyncio.create_task(phase4.maintain_background(statistics_stop))
            if phase4 is not None
            else None
        )
        try:
            yield
        finally:
            if statistics_task is not None:
                statistics_stop.set()
                await statistics_task
            if phase2 is not None:
                await phase2.aclose()

    application = FastAPI(
        title="Personal AI Provider Gateway Supervisor",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Return the Supervisor's local liveness state."""
        return {
            "service": "supervisor",
            "status": "ok",
            "phase": (
                "phase-6"
                if password_recovery is not None
                or sidecar_updates is not None
                or shutdown_request is not None
                else "phase-5" if phase5 is not None
                else "phase-4" if phase4 is not None else "phase-2"
            ),
        }

    if dashboard_password_bypass or (
        password_recovery is not None and admin_sessions is not None
    ):

        @application.get("/", response_class=HTMLResponse)
        async def dashboard_shell(request: Request) -> HTMLResponse:
            if dashboard_password_bypass:
                decision = authorize_control(request)
                allowed = (
                    await decision if inspect.isawaitable(decision) else decision
                )
                if allowed is not True:
                    raise HTTPException(
                        status_code=403, detail={"code": "control_forbidden"}
                    )
            html = DASHBOARD_HTML.replace(
                'data-control-mode="password"',
                (
                    'data-control-mode="direct"'
                    if dashboard_password_bypass
                    else 'data-control-mode="password"'
                ),
            )
            response = HTMLResponse(html)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; connect-src 'self'; img-src 'self'; "
                "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            return response

    if password_recovery is not None and admin_sessions is not None:

        @application.post("/api/control/password-recovery/challenge")
        async def start_password_recovery(
            request: Request,
            response: Response,
        ) -> dict[str, str]:
            if not admin_sessions.request_boundary_allows(request):
                raise HTTPException(
                    status_code=403, detail={"code": "control_forbidden"}
                )
            token = await password_recovery.start()
            if token is None:
                raise HTTPException(
                    status_code=403, detail={"code": "recovery_not_confirmed"}
                )
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return {"recovery_token": token}

        @application.post(
            "/api/control/password-recovery/complete",
            status_code=204,
        )
        async def complete_password_recovery(
            payload: PasswordRecoveryCompleteRequest,
            request: Request,
            response: Response,
        ) -> Response:
            if not admin_sessions.request_boundary_allows(request):
                raise HTTPException(
                    status_code=403, detail={"code": "control_forbidden"}
                )
            completed = password_recovery.complete(
                payload.recovery_token.get_secret_value(),
                payload.new_password.get_secret_value(),
            )
            if not completed:
                raise HTTPException(
                    status_code=400, detail={"code": "invalid_recovery_request"}
                )
            response.delete_cookie(
                admin_sessions.cookie_name,
                path="/api/control",
                httponly=True,
                samesite="strict",
            )
            response.status_code = 204
            return response

    if phase2 is not None and authorize_control is not None:

        async def require_control_access(request: Request) -> None:
            decision = authorize_control(request)
            allowed = await decision if inspect.isawaitable(decision) else decision
            if allowed is not True:
                raise HTTPException(status_code=403, detail={"code": "control_forbidden"})

        control_access = [Depends(require_control_access)]

        if admin_sessions is not None:

            @application.post("/api/control/session/login", status_code=204)
            async def login(
                credentials: AdminLoginRequest,
                request: Request,
                response: Response,
            ) -> Response:
                if not admin_sessions.request_boundary_allows(request):
                    raise HTTPException(
                        status_code=403, detail={"code": "control_forbidden"}
                    )
                token = admin_sessions.login(credentials.password.get_secret_value())
                if token is None:
                    raise HTTPException(
                        status_code=401, detail={"code": "invalid_credentials"}
                    )
                response.set_cookie(
                    key=admin_sessions.cookie_name,
                    value=token,
                    max_age=admin_sessions.max_age_seconds,
                    httponly=True,
                    samesite="strict",
                    secure=request.url.scheme == "https",
                    path="/api/control",
                )
                response.status_code = 204
                return response

            @application.post(
                "/api/control/session/logout",
                dependencies=control_access,
                status_code=204,
            )
            async def logout(request: Request, response: Response) -> Response:
                admin_sessions.logout(request)
                response.delete_cookie(
                    admin_sessions.cookie_name,
                    path="/api/control",
                    httponly=True,
                    samesite="strict",
                )
                response.status_code = 204
                return response

        @application.get("/api/control/sidecar/health", dependencies=control_access)
        async def sidecar_health() -> dict[str, object]:
            health = await _safe_call(phase2.sidecar_health)
            return {
                "state": health.state.value,
                "model_count": health.model_count,
                "quota_status": health.quota_status,
                "managed": health.managed,
            }

        @application.post("/api/control/sidecar/start", dependencies=control_access)
        async def start_sidecar() -> dict[str, object]:
            state = await _safe_call(phase2.start_sidecar)
            return _runtime_response(state)

        @application.post("/api/control/sidecar/stop", dependencies=control_access)
        async def stop_sidecar() -> dict[str, object]:
            state = await _safe_call(phase2.stop_sidecar)
            return _runtime_response(state)

        @application.post("/api/control/sidecar/restart", dependencies=control_access)
        async def restart_sidecar() -> dict[str, object]:
            state = await _safe_call(phase2.restart_sidecar)
            return _runtime_response(state)

        if antigravity_proxy is not None:

            @application.get(
                "/api/control/providers/antigravity/proxy",
                dependencies=control_access,
            )
            async def antigravity_proxy_status(response: Response) -> dict[str, bool]:
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                try:
                    return {"configured": antigravity_proxy.configured()}
                except PortableConfigurationError as error:
                    raise HTTPException(
                        409, detail={"code": "sidecar_configuration_mismatch"}
                    ) from error

            @application.post(
                "/api/control/providers/antigravity/proxy",
                dependencies=control_access,
            )
            async def replace_antigravity_proxy(
                payload: AntigravityProxyRequest,
                response: Response,
            ) -> dict[str, object]:
                try:
                    configured, state = await antigravity_proxy.replace(payload.proxy_url)
                except ValueError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_antigravity_proxy"}
                    ) from error
                except PortableConfigurationError as error:
                    raise HTTPException(
                        409, detail={"code": "sidecar_configuration_mismatch"}
                    ) from error
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                return {
                    "configured": configured,
                    "sidecar_state": state.state.value,
                }

        @application.post(
            "/api/control/providers/antigravity/oauth/start",
            dependencies=control_access,
        )
        async def start_antigravity_oauth() -> dict[str, str]:
            authorization = await _safe_call(phase2.start_antigravity_oauth)
            return {"url": authorization.url, "oauth_state": authorization.state}

        @application.post(
            "/api/control/providers/antigravity/oauth/status",
            dependencies=control_access,
        )
        async def antigravity_oauth_status(request: OAuthStatusRequest) -> dict[str, str]:
            result = await _safe_call(
                lambda: phase2.antigravity_oauth_status(
                    request.oauth_state.get_secret_value()
                )
            )
            return {"state": result.state.value}

        @application.post(
            "/api/control/providers/antigravity/models/discover",
            dependencies=control_access,
        )
        async def discover_antigravity_models() -> dict[str, object]:
            result = await _safe_call(phase2.discover_antigravity_models)
            return _discovery_response(result)

        @application.get("/api/control/models", dependencies=control_access)
        async def current_models() -> dict[str, object]:
            records = await _safe_call(_async_current_models)
            return {"data": [_model_response(record) for record in records]}

        async def _async_current_models() -> tuple[object, ...]:
            return phase2.current_models()

        if custom_providers is not None:

            @application.get(
                "/api/control/providers/openai-compatible",
                dependencies=control_access,
            )
            async def list_openai_compatible_providers(response: Response) -> dict[str, object]:
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                return {"data": [_custom_provider_metadata(item) for item in custom_providers.list_metadata()]}

            @application.post(
                "/api/control/providers/openai-compatible",
                dependencies=control_access,
                status_code=201,
            )
            async def replace_openai_compatible_provider(
                payload: CustomProviderRequest,
                response: Response,
            ) -> dict[str, object]:
                try:
                    api_key = (
                        payload.api_key.get_secret_value()
                        if payload.api_key is not None
                        else None
                    )
                    if api_key is None:
                        existing = await asyncio.to_thread(
                            custom_providers.load, payload.provider_id
                        )
                        if existing is not None:
                            api_key = existing.api_key
                    provider = OpenAICompatibleProvider(
                        provider_id=payload.provider_id,
                        display_name=payload.display_name,
                        base_url=payload.base_url,
                        api_key=api_key,
                        models=tuple(ProviderModel(item.model_id, item.display_name) for item in payload.models),
                        headers=tuple(ProviderHeader(item.name, item.value.get_secret_value()) for item in payload.headers),
                    )
                    metadata = await asyncio.to_thread(custom_providers.replace, provider)
                except CustomProviderError as error:
                    raise HTTPException(400, detail={"code": "invalid_custom_provider"}) from error
                probe = (
                    custom_provider_probe_factory(provider)
                    if custom_provider_probe_factory is not None
                    else OpenAICompatibleDataPlane(provider, timeout=15.0)
                )
                try:
                    discovered = await probe.discover_models()
                    metadata = await asyncio.to_thread(
                        custom_providers.refresh_discovered_models,
                        provider.provider_id,
                        discovered,
                    )
                except OpenAICompatibleProviderError as error:
                    metadata = await asyncio.to_thread(
                        custom_providers.record_verification_failure,
                        provider.provider_id,
                        error.category,
                    )
                except CustomProviderError:
                    metadata = await asyncio.to_thread(
                        custom_providers.record_verification_failure,
                        provider.provider_id,
                        "provider_protocol_error",
                    )
                finally:
                    await probe.aclose()
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                return _custom_provider_metadata(metadata)
        if phase4 is not None:

            @application.get("/api/control/usage", dependencies=control_access)
            async def usage_records(
                start: datetime | None = None,
                end: datetime | None = None,
                provider: str | None = None,
                model: str | None = None,
                client_key_id: str | None = None,
                limit: int = Query(default=100, ge=1, le=1000),
            ) -> dict[str, object]:
                query = UsageQuery(start, end, provider, model, client_key_id)
                try:
                    records = phase4.usage_records(query)[:limit]
                except ValueError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_statistics_filter"}
                    ) from error
                return {"data": [_usage_response(record) for record in records]}

            @application.get(
                "/api/control/statistics/summary", dependencies=control_access
            )
            async def statistics_summary(
                start: datetime | None = None,
                end: datetime | None = None,
                provider: str | None = None,
                model: str | None = None,
                client_key_id: str | None = None,
            ) -> dict[str, object]:
                query = UsageQuery(start, end, provider, model, client_key_id)
                try:
                    usage, health, quota = await phase4.dashboard_snapshot(query)
                except ValueError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_statistics_filter"}
                    ) from error
                return {
                    "usage": _usage_summary_response(usage),
                    "health": _health_response(health),
                    "quota": _quota_collection_response(quota),
                }

            @application.get(
                "/api/control/providers/antigravity/health",
                dependencies=control_access,
            )
            async def antigravity_health() -> dict[str, object]:
                return _health_response(await phase4.provider_health())

            @application.get(
                "/api/control/providers/antigravity/quota",
                dependencies=control_access,
            )
            async def antigravity_quota() -> dict[str, object]:
                return _quota_collection_response(await phase4.quota_snapshots())

            @application.post(
                "/api/control/providers/antigravity/quota/refresh",
                dependencies=control_access,
            )
            async def refresh_antigravity_quota() -> dict[str, object]:
                return _quota_collection_response(
                    await phase4.quota_snapshots(force=True)
                )

            @application.post(
                "/api/control/usage/retention/run",
                dependencies=control_access,
            )
            async def run_usage_retention() -> dict[str, int]:
                return {"deleted": phase4.purge_usage()}

        if phase5 is not None:

            @application.get(
                "/api/control/integrations/export-providers",
                dependencies=control_access,
            )
            async def export_provider_selection() -> dict[str, object]:
                return {"data": phase5.export_provider_options()}

            @application.post(
                "/api/control/integrations/export-providers",
                dependencies=control_access,
            )
            async def replace_export_provider_selection(
                payload: ExportProviderSelectionRequest,
            ) -> dict[str, object]:
                try:
                    options = phase5.replace_export_providers(
                        set(payload.provider_ids)
                    )
                except IntegrationConfigError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_harness_configuration"}
                    ) from error
                return {"data": options}

            @application.get(
                "/api/control/integrations/opencode/config",
                dependencies=control_access,
            )
            async def opencode_config(
                provider_id: str = "personal-ai-gateway",
                default_model: str | None = None,
                api_key_env: str = "AIPG_GATEWAY_API_KEY",
            ) -> dict[str, object]:
                try:
                    return phase5.opencode_config(
                        provider_id=provider_id,
                        default_model=default_model,
                        api_key_env=api_key_env,
                    )
                except IntegrationConfigError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_harness_configuration"}
                    ) from error

            @application.get(
                "/api/control/integrations/cline/config",
                dependencies=control_access,
            )
            async def cline_config(
                model: str | None = None,
                api_key_env: str = "AIPG_GATEWAY_API_KEY",
            ) -> dict[str, object]:
                try:
                    return phase5.cline_config(
                        model=model,
                        api_key_env=api_key_env,
                    )
                except IntegrationConfigError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_harness_configuration"}
                    ) from error

        if sidecar_updates is not None:

            @application.get(
                "/api/control/sidecar/updates",
                dependencies=control_access,
            )
            async def sidecar_update_candidates() -> dict[str, object]:
                return {"data": sidecar_updates.list_candidates()}

            @application.post(
                "/api/control/sidecar/updates/install",
                dependencies=control_access,
            )
            async def install_sidecar_update(
                payload: SidecarInstallRequest,
            ) -> dict[str, object]:
                try:
                    result = await sidecar_updates.install(payload.candidate_id)
                except SidecarUpdateError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_sidecar_update"}
                    ) from error
                return _sidecar_update_response(result)

            @application.post(
                "/api/control/sidecar/updates/rollback",
                dependencies=control_access,
            )
            async def rollback_sidecar_update() -> dict[str, object]:
                try:
                    result = await sidecar_updates.rollback()
                except SidecarUpdateError as error:
                    raise HTTPException(
                        409, detail={"code": "sidecar_rollback_unavailable"}
                    ) from error
                return _sidecar_update_response(result)

        if shutdown_request is not None:

            @application.post(
                "/api/control/application/shutdown",
                dependencies=control_access,
                status_code=202,
            )
            async def shutdown_application() -> dict[str, str]:
                shutdown_request.request()
                return {"status": "shutting_down"}

        if gateway_keys is not None:

            @application.get(
                "/api/control/gateway-keys",
                dependencies=control_access,
            )
            async def list_gateway_keys(response: Response) -> dict[str, object]:
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                return {
                    "data": [
                        {
                            "key_id": item.key_id,
                            "label": item.key_id,
                            "prefix": item.prefix,
                            "created_at": item.created_at.isoformat(),
                            "last_used_at": (
                                item.last_used_at.isoformat()
                                if item.last_used_at is not None
                                else None
                            ),
                            "status": "active" if item.active else "deleted",
                        }
                        for item in await asyncio.to_thread(
                            gateway_keys.list_metadata
                        )
                    ]
                }

            @application.post(
                "/api/control/gateway-keys",
                dependencies=control_access,
                status_code=201,
            )
            async def create_gateway_key(
                payload: GatewayKeyCreateRequest,
                response: Response,
            ) -> dict[str, str]:
                try:
                    issued = await asyncio.to_thread(gateway_keys.create, payload.label)
                except GatewayKeyError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_gateway_key_request"}
                    ) from error
                response.headers["Cache-Control"] = "no-store"
                response.headers["Pragma"] = "no-cache"
                return {
                    "key_id": issued.key_id,
                    "prefix": issued.prefix,
                    "secret": issued.secret,
                    "created_at": issued.created_at.isoformat(),
                }

            @application.delete(
                "/api/control/gateway-keys/{key_id}",
                dependencies=control_access,
                status_code=204,
            )
            async def delete_gateway_key(key_id: str, response: Response) -> Response:
                try:
                    deleted = await asyncio.to_thread(gateway_keys.revoke, key_id)
                except GatewayKeyError as error:
                    raise HTTPException(
                        400, detail={"code": "invalid_gateway_key_request"}
                    ) from error
                if not deleted:
                    raise HTTPException(
                        404, detail={"code": "gateway_key_not_active"}
                    )
                response.headers["Cache-Control"] = "no-store"
                response.status_code = 204
                return response

    return application


async def _safe_call(operation: Callable[[], Awaitable[object]]) -> object:
    try:
        return await operation()
    except SidecarAuthenticationError as error:
        raise HTTPException(502, detail={"code": "sidecar_auth_failed"}) from error
    except SidecarTransportError as error:
        raise HTTPException(503, detail={"code": "sidecar_unavailable"}) from error
    except SidecarProtocolError as error:
        raise HTTPException(502, detail={"code": "invalid_sidecar_response"}) from error
    except SidecarConfigurationError as error:
        raise HTTPException(400, detail={"code": "invalid_control_request"}) from error
    except FileNotFoundError as error:
        raise HTTPException(503, detail={"code": "sidecar_not_configured"}) from error
    except ModelRegistryError as error:
        raise HTTPException(409, detail={"code": "model_registry_conflict"}) from error


def _runtime_response(state: object) -> dict[str, object]:
    return {
        "state": state.state.value,
        "pid": state.pid,
        "exit_code": state.exit_code,
    }


def _model_response(record: object) -> dict[str, object]:
    return {
        "id": record.canonical_id,
        "provider": record.provider,
        "upstream_id": record.upstream_id,
        "display_name": record.display_name,
        "alias": record.alias,
        "available": record.available,
        "stale": record.discovery_state.value == "stale",
        "last_discovered_at": record.discovered_at.isoformat(),
        "last_success_at": (
            record.last_success_at.isoformat()
            if record.last_success_at is not None
            else None
        ),
    }


def _discovery_response(result: object) -> dict[str, object]:
    return {
        "state": result.state.value,
        "stale": result.state.value == "stale",
        "last_success_at": (
            result.last_success_at.isoformat()
            if result.last_success_at is not None
            else None
        ),
        "error_category": result.error_category,
        "models": [_model_response(record) for record in result.models],
    }


def _usage_response(record: object) -> dict[str, object]:
    return {
        "request_id": record.request_id,
        "timestamp": record.timestamp.isoformat(),
        "client_key_id": record.client_key_id,
        "provider": record.provider,
        "canonical_model": record.canonical_model,
        "stream": record.stream,
        "status": record.status.value,
        "latency_ms": record.latency_ms,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "total_tokens": record.total_tokens,
        "token_source": record.token_source.value,
        "error_category": record.error_category,
    }


def _usage_summary_response(summary: object) -> dict[str, object]:
    if summary.request_count == 0 or summary.unknown_token_count == summary.request_count:
        token_status = "unknown"
    elif summary.unknown_token_count:
        token_status = "partial"
    else:
        token_status = "available"
    return {
        "request_count": summary.request_count,
        "succeeded_count": summary.succeeded_count,
        "failed_count": summary.failed_count,
        "aborted_count": summary.aborted_count,
        "input_tokens": summary.input_tokens,
        "output_tokens": summary.output_tokens,
        "total_tokens": summary.total_tokens,
        "token_status": token_status,
        "unknown_token_count": summary.unknown_token_count,
        "average_latency_ms": summary.average_latency_ms,
    }


def _health_response(health: object) -> dict[str, object]:
    return {
        "provider": health.provider,
        "state": health.state.value,
        "checked_at": health.checked_at.isoformat(),
        "latency_ms": health.latency_ms,
        "error_category": health.error_category,
    }


def _quota_collection_response(snapshots: list[object]) -> dict[str, object]:
    available = any(snapshot.status.value == "available" for snapshot in snapshots)
    return {
        "status": "available" if available else "unknown",
        "data": [
            {
                "provider": snapshot.provider,
                "bucket_id": snapshot.bucket_id,
                "status": snapshot.status.value,
                "group_display": snapshot.group_display,
                "bucket_display": snapshot.bucket_display,
                "window": snapshot.window,
                "remaining_ratio": snapshot.remaining_ratio,
                "used_ratio": snapshot.used_ratio,
                "reset_at": (
                    snapshot.reset_at.isoformat()
                    if snapshot.reset_at is not None
                    else None
                ),
                "source": snapshot.source,
                "updated_at": snapshot.updated_at.isoformat(),
                "error_category": snapshot.error_category,
            }
            for snapshot in snapshots
        ],
    }


def _sidecar_update_response(result: object) -> dict[str, object]:
    return {
        "status": result.status,
        "message": result.message,
        "version": result.version,
    }

def _custom_provider_metadata(item: object) -> dict[str, object]:
    return {
        "provider_id": item.provider_id,
        "display_name": item.display_name,
        "base_url": item.base_url,
        "api_key_configured": item.api_key_configured,
        "header_names": list(item.header_names),
        "model_count": item.model_count,
        "verification_status": item.verification_status,
        "verification_error": item.verification_error,
    }

app = create_app()
