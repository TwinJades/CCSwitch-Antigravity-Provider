"""Authenticated Supervisor facade for Phase 2 control-plane operations."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ModelRecord, ModelRegistry
from ..providers.antigravity import AntigravityDiscovery, DiscoveryOutcome
from ..sidecars.cliproxy.client import (
    CLIProxyAPIClient,
    OAuthAuthorization,
    OAuthStatus,
)
from ..sidecars.cliproxy.health import SidecarHealth, SidecarHealthChecker
from ..sidecars.cliproxy.lifecycle import CLIProxyAPILifecycle, SidecarRuntimeState


@dataclass(slots=True)
class Phase2Control:
    """Coordinate Sidecar lifecycle, OAuth, health and model discovery."""

    lifecycle: CLIProxyAPILifecycle
    client: CLIProxyAPIClient
    health_checker: SidecarHealthChecker
    discovery: AntigravityDiscovery
    registry: ModelRegistry

    async def start_sidecar(self) -> SidecarRuntimeState:
        return await self.lifecycle.start()

    async def stop_sidecar(self) -> SidecarRuntimeState:
        return await self.lifecycle.stop()

    async def restart_sidecar(self) -> SidecarRuntimeState:
        return await self.lifecycle.restart()

    async def sidecar_health(self) -> SidecarHealth:
        return await self.health_checker.check()

    async def start_antigravity_oauth(self) -> OAuthAuthorization:
        return await self.client.get_antigravity_auth_url()

    async def antigravity_oauth_status(self, state: str) -> OAuthStatus:
        return await self.client.get_auth_status(state)

    async def discover_antigravity_models(self) -> DiscoveryOutcome:
        return await self.discovery.refresh()

    def current_models(self) -> tuple[ModelRecord, ...]:
        """Return the shared catalogue across every configured Provider."""

        return tuple(self.registry.list_models())

    async def aclose(self) -> None:
        try:
            await self.lifecycle.stop()
        finally:
            await self.client.aclose()
