"""CLIProxyAPI lifecycle, management, and health adapters."""

from .client import CLIProxyAPIClient, ManagementKey, SidecarApiKey
from .data_plane import CLIProxyAPIDataPlaneClient, SidecarUpstreamError
from .health import SidecarHealth, SidecarHealthChecker, SidecarHealthState
from .lifecycle import (
    CLIProxyAPILifecycle,
    SidecarLaunchSpec,
    SidecarProcessState,
    SidecarRuntimeState,
)

__all__ = [
    "CLIProxyAPIClient",
    "CLIProxyAPIDataPlaneClient",
    "CLIProxyAPILifecycle",
    "ManagementKey",
    "SidecarApiKey",
    "SidecarUpstreamError",
    "SidecarHealth",
    "SidecarHealthChecker",
    "SidecarHealthState",
    "SidecarLaunchSpec",
    "SidecarProcessState",
    "SidecarRuntimeState",
]
