"""Map CLIProxyAPI's dynamic catalogue into the durable model registry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ...models import (
    DiscoveredModel,
    DiscoveryState,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
)
from ...sidecars.cliproxy.client import (
    ModelDiscovery,
    SidecarAuthenticationError,
    SidecarProtocolError,
    SidecarTransportError,
)


ANTIGRAVITY_PROVIDER = "antigravity"


class ModelDiscoveryClient(Protocol):
    async def discover_models(self) -> ModelDiscovery: ...


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """Safe discovery result suitable for the authenticated control plane."""

    state: DiscoveryState
    models: tuple[ModelRecord, ...]
    last_success_at: datetime | None
    error_category: str | None


class AntigravityDiscovery:
    """Refresh Antigravity models without hard-coded model identifiers."""

    def __init__(self, client: ModelDiscoveryClient, registry: ModelRegistry) -> None:
        self._client = client
        self._registry = registry

    async def refresh(self) -> DiscoveryOutcome:
        try:
            sidecar_result = await self._client.discover_models()
        except SidecarAuthenticationError:
            return self._failure("sidecar_api_auth_failed")
        except SidecarTransportError:
            return self._failure("sidecar_unavailable")
        except SidecarProtocolError:
            return self._failure("invalid_sidecar_response")

        if not sidecar_result.models:
            return self._failure("empty_catalog")

        discovered = [
            DiscoveredModel(upstream_id=model.model_id, display_name=model.model_id)
            for model in sidecar_result.models
        ]
        try:
            records = self._registry.refresh_success(ANTIGRAVITY_PROVIDER, discovered)
        except ModelRegistryError:
            return self._failure("invalid_model_catalog")
        snapshot = self._registry.discovery_state(ANTIGRAVITY_PROVIDER)
        if snapshot is None:
            raise RuntimeError("registry did not persist discovery state")
        return DiscoveryOutcome(
            state=snapshot.state,
            models=tuple(records),
            last_success_at=snapshot.last_success_at,
            error_category=None,
        )

    def current(self) -> DiscoveryOutcome:
        snapshot = self._registry.discovery_state(ANTIGRAVITY_PROVIDER)
        records = tuple(self._registry.list_models(ANTIGRAVITY_PROVIDER))
        if snapshot is None:
            return DiscoveryOutcome(
                state=DiscoveryState.STALE,
                models=records,
                last_success_at=None,
                error_category="not_discovered",
            )
        return DiscoveryOutcome(
            state=snapshot.state,
            models=records,
            last_success_at=snapshot.last_success_at,
            error_category=snapshot.error_category,
        )

    def _failure(self, category: str) -> DiscoveryOutcome:
        self._registry.record_failure(ANTIGRAVITY_PROVIDER, category)
        return self.current()
