"""Explicit Phase 2 dependency composition without import-time side effects."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets

from ..database import create_database_engine
from ..health import ProviderHealthStore
from ..models import ModelRegistry
from ..portable import (
    PortableConfigurationError,
    PortableLayout,
    RuntimeSecrets,
    WindowsDataProtector,
    antigravity_proxy_url,
    normalize_antigravity_proxy_url,
    write_antigravity_proxy_url,
)
from ..providers.openai_compatible import CustomProviderStore
from ..providers.antigravity import AntigravityDiscovery
from ..providers.antigravity.quota import AntigravityQuotaProbe
from ..quota import QuotaCache, QuotaStore
from ..sidecars.cliproxy.client import (
    CLIProxyAPIClient,
    ManagementKey,
    SidecarApiKey,
    SidecarClientError,
)
from ..sidecars.cliproxy.health import SidecarHealthChecker
from ..sidecars.cliproxy.lifecycle import (
    CLIProxyAPILifecycle,
    SidecarLaunchSpec,
    SidecarProcessState,
    SidecarRuntimeState,
)
from .phase2 import Phase2Control
from .phase4 import Phase4Control
from .phase5 import Phase5Control
from ..usage import UsageRecorder


@dataclass(frozen=True, slots=True)
class Phase2RuntimeSettings:
    """Paths and credentials required to activate Phase 2 control services."""

    executable: Path
    config_path: Path
    database_url: str
    base_url: str = "http://127.0.0.1:8317"
    api_key: str = field(default="", repr=False)
    management_key: str = field(default="", repr=False)
    gateway_base_url: str = "http://127.0.0.1:8020/v1"

    def __post_init__(self) -> None:
        if not self.api_key or not self.management_key:
            raise ValueError("Both Sidecar credential domains are required.")
        if secrets.compare_digest(self.api_key, self.management_key):
            raise ValueError("Sidecar API and Management credentials must be different.")

    @classmethod
    def from_env(cls) -> "Phase2RuntimeSettings":
        required = {
            "executable": os.getenv("AIPG_SIDECAR_EXECUTABLE", ""),
            "config_path": os.getenv("AIPG_SIDECAR_CONFIG", ""),
            "api_key": os.getenv("AIPG_SIDECAR_API_KEY", ""),
            "management_key": os.getenv("AIPG_SIDECAR_MANAGEMENT_KEY", ""),
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(
                "Phase 2 runtime configuration is incomplete: "
                + ", ".join(sorted(missing))
            )
        return cls(
            executable=Path(required["executable"]),
            config_path=Path(required["config_path"]),
            database_url=os.getenv(
                "AIPG_DATABASE_URL", "sqlite:///./data/gateway.db"
            ),
            base_url=os.getenv(
                "AIPG_SIDECAR_BASE_URL", "http://127.0.0.1:8317"
            ),
            api_key=required["api_key"],
            management_key=required["management_key"],
            gateway_base_url=os.getenv(
                "AIPG_GATEWAY_BASE_URL", "http://127.0.0.1:8020/v1"
            ),
        )


@dataclass(slots=True)
class AntigravityProxyControl:
    """Persist and apply the optional Sidecar-only local HTTP proxy."""

    layout: PortableLayout
    runtime: RuntimeSecrets
    phase2: Phase2Control

    def configured(self) -> bool:
        return antigravity_proxy_url(self.layout, self.runtime) is not None

    async def replace(self, value: str | None) -> tuple[bool, SidecarRuntimeState]:
        normalized = normalize_antigravity_proxy_url(value)
        previous = antigravity_proxy_url(self.layout, self.runtime)
        await self.phase2.stop_sidecar()
        try:
            configured = write_antigravity_proxy_url(
                self.layout, self.runtime, normalized
            )
        except Exception:
            await self.phase2.start_sidecar()
            raise
        state = await self.phase2.start_sidecar()
        if state.state is SidecarProcessState.CRASHED:
            await self.phase2.stop_sidecar()
            write_antigravity_proxy_url(self.layout, self.runtime, previous)
            await self.phase2.start_sidecar()
            raise PortableConfigurationError(
                "Sidecar rejected the Antigravity proxy configuration."
            )
        return configured, state


def build_phase2_control(settings: Phase2RuntimeSettings) -> Phase2Control:
    """Compose Phase 2 services; callers still must supply HTTP authorization."""

    client = CLIProxyAPIClient(
        base_url=settings.base_url,
        api_key=SidecarApiKey(settings.api_key),
        management_key=ManagementKey(settings.management_key),
    )

    async def readiness_probe() -> bool:
        try:
            await client.discover_models()
        except SidecarClientError:
            return False
        return True

    lifecycle = CLIProxyAPILifecycle(
        SidecarLaunchSpec(
            executable=settings.executable,
            config_path=settings.config_path,
        ),
        readiness_probe=readiness_probe,
        readiness_attempts=25,
        readiness_interval=0.2,
    )
    registry = ModelRegistry(create_database_engine(settings.database_url))
    discovery = AntigravityDiscovery(client, registry)
    return Phase2Control(
        lifecycle=lifecycle,
        client=client,
        health_checker=SidecarHealthChecker(lifecycle, client),
        discovery=discovery,
        registry=registry,
    )


def build_phase4_control(
    settings: Phase2RuntimeSettings, phase2: Phase2Control
) -> Phase4Control:
    """Compose statistics stores without adding them to the inference path."""

    engine = create_database_engine(settings.database_url)
    usage = UsageRecorder(engine)
    return Phase4Control(
        phase2=phase2,
        usage=usage,
        quota=QuotaCache(QuotaStore(engine), AntigravityQuotaProbe()),
        health=ProviderHealthStore(engine),
    )


def build_custom_provider_store(settings: Phase2RuntimeSettings) -> CustomProviderStore:
    """Use current-user DPAPI for custom provider credentials."""

    return CustomProviderStore(
        create_database_engine(settings.database_url), WindowsDataProtector()
    )


def build_antigravity_proxy_control(
    settings: Phase2RuntimeSettings, phase2: Phase2Control
) -> AntigravityProxyControl:
    config_path = settings.config_path.resolve()
    layout = PortableLayout(config_path.parent.parent)
    if layout.sidecar_config.resolve() != config_path:
        raise PortableConfigurationError("Portable Sidecar config path is invalid.")
    return AntigravityProxyControl(
        layout=layout,
        runtime=RuntimeSecrets(settings.api_key, settings.management_key),
        phase2=phase2,
    )


def build_phase5_control(
    settings: Phase2RuntimeSettings, phase2: Phase2Control
) -> Phase5Control:
    """Compose Harness generators without loading a Gateway API-key value."""

    from ..integrations import HarnessExportSelectionStore

    return Phase5Control(
        phase2=phase2,
        gateway_base_url=settings.gateway_base_url,
        export_selection=HarnessExportSelectionStore(
            create_database_engine(settings.database_url)
        ),
    )
