"""Sidecar health classification without conflating health, models, and quota."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .client import (
    CLIProxyAPIClient,
    SidecarAuthenticationError,
    SidecarClientError,
)
from .lifecycle import CLIProxyAPILifecycle, SidecarProcessState


class SidecarHealthState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    AVAILABLE = "available"
    AUTH_REQUIRED = "auth_required"
    API_AUTH_FAILED = "api_auth_failed"
    UNAVAILABLE = "unavailable"
    CRASHED = "crashed"


@dataclass(frozen=True, slots=True)
class SidecarHealth:
    """Health result; model count is informational and never represents quota."""

    state: SidecarHealthState
    model_count: int | None
    quota_status: str = "unknown"
    managed: bool = True


class SidecarHealthChecker:
    """Combine owned-process state with safe data-plane and auth-file observations."""

    def __init__(self, lifecycle: CLIProxyAPILifecycle, client: CLIProxyAPIClient) -> None:
        self._lifecycle = lifecycle
        self._client = client

    async def check(self) -> SidecarHealth:
        runtime = await self._lifecycle.status()
        if runtime.state is SidecarProcessState.STOPPED:
            return await self._probe_api(
                managed=False,
                transport_state=SidecarHealthState.STOPPED,
            )
        if runtime.state is SidecarProcessState.CRASHED:
            return SidecarHealth(SidecarHealthState.CRASHED, None)
        if runtime.state is SidecarProcessState.STARTING:
            return await self._probe_api(
                managed=True,
                transport_state=SidecarHealthState.STARTING,
            )
        return await self._probe_api(
            managed=True,
            transport_state=SidecarHealthState.UNAVAILABLE,
        )

    async def _probe_api(
        self,
        *,
        managed: bool,
        transport_state: SidecarHealthState,
    ) -> SidecarHealth:
        try:
            discovery = await self._client.discover_models()
        except SidecarAuthenticationError:
            return SidecarHealth(
                SidecarHealthState.API_AUTH_FAILED, None, managed=managed
            )
        except SidecarClientError:
            return SidecarHealth(
                transport_state, None, managed=managed
            )
        if discovery.models:
            return SidecarHealth(
                SidecarHealthState.AVAILABLE,
                len(discovery.models),
                managed=managed,
            )
        try:
            auth_files = await self._client.get_auth_file_summary()
        except SidecarAuthenticationError:
            return SidecarHealth(
                SidecarHealthState.API_AUTH_FAILED, 0, managed=managed
            )
        except SidecarClientError:
            # The data plane is reachable. A management-plane failure alone is not quota.
            return SidecarHealth(
                SidecarHealthState.AVAILABLE, 0, managed=managed
            )
        # A present auth file with an unrecognised status is not evidence that
        # the user is unauthenticated. CLIProxyAPI versions may omit or rename
        # their per-file status field while the credential remains usable.
        # Only an actually empty Antigravity auth-file list means login is
        # required; otherwise the reachable data plane is connected but has no
        # models to report yet.
        if auth_files.total == 0:
            return SidecarHealth(
                SidecarHealthState.AUTH_REQUIRED, 0, managed=managed
            )
        return SidecarHealth(SidecarHealthState.AVAILABLE, 0, managed=managed)
