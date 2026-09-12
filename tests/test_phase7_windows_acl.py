from __future__ import annotations

from pathlib import Path

import pytest

import ai_provider_gateway.portable as portable
from ai_provider_gateway.portable import (
    PortableConfigurationError,
    PortableLayout,
    PortableSecretStore,
    RuntimeSecrets,
    secure_windows_path,
    write_sidecar_config,
)


class _Protector:
    def protect(self, value: bytes) -> bytes:
        return value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value[::-1]


class _AclRecorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.paths: list[Path] = []
        self.error = error

    def protect(self, path: Path) -> None:
        self.paths.append(path)
        if self.error:
            raise self.error


def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(portable.os, "name", "nt")


def test_layout_secures_each_secret_or_state_directory_on_windows(monkeypatch, tmp_path):
    _windows(monkeypatch)
    protected: list[Path] = []
    monkeypatch.setattr(portable, "secure_windows_path", protected.append)
    layout = PortableLayout(tmp_path / "bundle")

    layout.ensure_directories()

    assert protected == [
        layout.data_dir,
        layout.logs_dir,
        layout.runtime_dir,
        layout.config_dir,
        layout.auth_dir,
        layout.staging_dir,
    ]


def test_acl_api_failure_is_fail_closed(monkeypatch, tmp_path):
    _windows(monkeypatch)
    path = tmp_path / "state"
    path.mkdir()
    recorder = _AclRecorder(PortableConfigurationError("ACL failure"))

    with pytest.raises(PortableConfigurationError, match="ACL failure"):
        secure_windows_path(path, recorder)


def test_reparse_point_is_rejected_before_acl_api(monkeypatch, tmp_path):
    _windows(monkeypatch)
    path = tmp_path / "state"
    path.mkdir()
    recorder = _AclRecorder()
    monkeypatch.setattr(portable, "_is_reparse_point", lambda _: True)

    with pytest.raises(PortableConfigurationError, match="cannot be secured"):
        secure_windows_path(path, recorder)
    assert recorder.paths == []


def test_secret_and_sidecar_files_are_secured_before_they_are_read_or_created(monkeypatch, tmp_path):
    _windows(monkeypatch)
    protected: list[Path] = []
    monkeypatch.setattr(portable, "secure_windows_path", protected.append)
    secret_path = tmp_path / "data" / "runtime-secrets.dat"
    PortableSecretStore(secret_path, _Protector()).load_or_create()

    layout = PortableLayout(tmp_path / "bundle")
    layout.ensure_directories()
    write_sidecar_config(layout, RuntimeSecrets("s" * 40, "m" * 40))

    assert secret_path.parent in protected
    assert secret_path in protected
    assert layout.sidecar_config.parent in protected
    assert layout.sidecar_config in protected
