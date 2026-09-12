"""Production dependency composition for the Gateway process only."""

from __future__ import annotations

from dataclasses import dataclass, field
import os

from ..database import create_database_engine
from ..models import ModelRegistry
from ..portable import WindowsDataProtector
from ..providers.openai_compatible import CustomProviderStore
from ..providers.antigravity import AntigravityDiscovery
from ..sidecars.cliproxy.client import SidecarApiKey
from ..sidecars.cliproxy.data_plane import CLIProxyAPIDataPlaneClient
from ..usage import UsageRecorder
from .app import GatewayService
from .authentication import GatewayApiKeyStore


@dataclass(frozen=True, slots=True)
class GatewayRuntimeSettings:
    """The Gateway receives no control-plane or OAuth credentials."""

    database_url: str
    sidecar_base_url: str = "http://127.0.0.1:8317"
    sidecar_api_key: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not self.database_url.strip() or not self.sidecar_api_key.strip():
            raise ValueError("Gateway runtime configuration is incomplete.")

    @classmethod
    def from_env(cls) -> "GatewayRuntimeSettings":
        database_url = os.getenv("AIPG_DATABASE_URL", "").strip()
        sidecar_api_key = os.getenv("AIPG_SIDECAR_API_KEY", "")
        if not database_url or not sidecar_api_key.strip():
            raise ValueError("Gateway runtime configuration is incomplete.")
        return cls(
            database_url=database_url,
            sidecar_base_url=os.getenv("AIPG_SIDECAR_BASE_URL", "http://127.0.0.1:8317"),
            sidecar_api_key=sidecar_api_key,
        )


def build_gateway_service(settings: GatewayRuntimeSettings) -> GatewayService:
    """Build the data plane without importing Supervisor runtime composition."""

    engine = create_database_engine(settings.database_url)
    registry = ModelRegistry(engine)
    key_store = GatewayApiKeyStore(engine)
    usage_recorder = UsageRecorder(engine)
    custom_providers = CustomProviderStore(engine, WindowsDataProtector())
    data_plane = CLIProxyAPIDataPlaneClient(
        base_url=settings.sidecar_base_url,
        api_key=SidecarApiKey(settings.sidecar_api_key),
    )

    discovery = AntigravityDiscovery(data_plane, registry)

    async def refresh_antigravity_models() -> None:
        """Reuse Phase 2 discovery so failures persist stale state safely."""

        await discovery.refresh()

    return GatewayService(
        registry=registry,
        data_plane=data_plane,
        authorize=key_store.authenticate,
        refresh_models=refresh_antigravity_models,
        usage_recorder=usage_recorder,
        custom_providers=custom_providers,
    )
