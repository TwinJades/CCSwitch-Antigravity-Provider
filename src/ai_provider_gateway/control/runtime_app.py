"""Production Phase 2 Supervisor application factory."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from fastapi import FastAPI

from ..database import create_database_engine
from ..gateway.authentication import GatewayApiKeyStore
from ..sidecars.cliproxy.updater import SidecarUpdateManager
from .app import create_app
from .auth import AdminSessionManager, authorize_local_control
from .password_recovery import (
    AdminPasswordStore,
    PasswordRecoveryManager,
    WindowsMessageBoxConfirmation,
)
from .runtime import (
    Phase2RuntimeSettings,
    build_phase2_control,
    build_phase4_control,
    build_phase5_control,
    build_antigravity_proxy_control,
    build_custom_provider_store,
)
from .shutdown import ShutdownRequest


def create_runtime_app() -> FastAPI:
    """Build the authenticated local Supervisor from process settings."""

    encoded_password_verifier = os.getenv("AIPG_ADMIN_PASSWORD_VERIFIER", "")
    settings = Phase2RuntimeSettings.from_env()
    phase2 = build_phase2_control(settings)
    phase4 = build_phase4_control(settings, phase2)
    phase5 = build_phase5_control(settings, phase2)
    custom_providers = build_custom_provider_store(settings)
    phase6_enabled = os.getenv("AIPG_PHASE6_ENABLED") == "1"
    password_recovery = None
    sidecar_updates = None
    shutdown_request = None
    gateway_keys = None
    antigravity_proxy = None
    dashboard_password_bypass = phase6_enabled and (
        os.getenv("AIPG_DASHBOARD_PASSWORD_BYPASS", "1") == "1"
    )

    if phase6_enabled:
        antigravity_proxy = build_antigravity_proxy_control(settings, phase2)
        engine = create_database_engine(settings.database_url)
        gateway_keys = GatewayApiKeyStore(engine)
        if dashboard_password_bypass:
            sessions = None
        else:
            password_store = AdminPasswordStore(engine)
            stored_verifier = password_store.current_verifier()
            if stored_verifier is None:
                if not encoded_password_verifier:
                    raise ValueError("Administrator password verifier is not initialized.")
                stored_verifier = password_store.initialize_if_missing(
                    encoded_password_verifier
                )
            sessions = AdminSessionManager(stored_verifier)
            password_recovery = PasswordRecoveryManager(
                password_store,
                sessions,
                WindowsMessageBoxConfirmation(),
            )
        required_paths = {
            "manifest": os.getenv("AIPG_SIDECAR_MANIFEST", ""),
            "staging": os.getenv("AIPG_SIDECAR_STAGING_ROOT", ""),
            "backup": os.getenv("AIPG_SIDECAR_BACKUP_ROOT", ""),
            "shutdown": os.getenv("AIPG_SHUTDOWN_REQUEST_PATH", ""),
        }
        if any(not value.strip() for value in required_paths.values()):
            raise ValueError("Phase 6 runtime paths are incomplete.")
        sidecar_updates = SidecarUpdateManager(
            settings.executable,
            Path(required_paths["manifest"]),
            Path(required_paths["staging"]),
            Path(required_paths["backup"]),
            phase2.lifecycle,
            _confirm_sidecar_mutation,
        )
        shutdown_request = ShutdownRequest(Path(required_paths["shutdown"]))
    else:
        if not encoded_password_verifier:
            raise ValueError("Administrator password verifier is required.")
        sessions = AdminSessionManager(encoded_password_verifier)
    return create_app(
        phase2=phase2,
        phase4=phase4,
        phase5=phase5,
        authorize_control=(
            authorize_local_control
            if dashboard_password_bypass
            else sessions.authorize
        ),
        admin_sessions=sessions,
        password_recovery=password_recovery,
        sidecar_updates=sidecar_updates,
        shutdown_request=shutdown_request,
        gateway_keys=gateway_keys,
        custom_providers=custom_providers,
        antigravity_proxy=antigravity_proxy,
        dashboard_password_bypass=dashboard_password_bypass,
        auto_start_sidecar=phase6_enabled
        and os.getenv("AIPG_AUTOSTART_SIDECAR") == "1",
    )


def _confirm_sidecar_mutation(label: str) -> bool:
    if os.name != "nt":
        return False
    result = ctypes.windll.user32.MessageBoxW(
        None,
        f"Allow this local operation?\n\n{label}",
        "AI Provider Gateway Sidecar update",
        0x00000004 | 0x00000030,
    )
    return result == 6
