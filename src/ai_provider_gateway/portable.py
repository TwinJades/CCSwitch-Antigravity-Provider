"""Portable Windows layout and first-run bootstrap without credential backup."""

from __future__ import annotations

import base64
import bcrypt
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable, Protocol
from urllib.parse import urlsplit, urlunsplit

from alembic import command
from alembic.config import Config
from .database import create_database_engine
from .security import generate_secret, hash_password
from .sidecars.cliproxy.updater import SidecarManifest, verify_sha256


class PortableConfigurationError(RuntimeError):
    """A safe local bootstrap or portable-layout error."""


_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_reparse_point(path: Path) -> bool:
    """Return whether *path* is a link/reparse point without following it."""

    try:
        status = path.lstat()
    except OSError as error:
        raise PortableConfigurationError("Local runtime path cannot be secured.") from error
    return path.is_symlink() or bool(
        getattr(status, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


class WindowsAclProtector:
    """Apply an owner/SYSTEM/Administrators-only DACL to a local path."""

    _token_query = 0x0008
    _token_user = 1
    _error_insufficient_buffer = 122
    _dacl_security_information = 0x00000004
    _protected_dacl_security_information = 0x80000000

    def __init__(self) -> None:
        if os.name != "nt":
            raise PortableConfigurationError("Windows ACL protection is required.")
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32.OpenProcessToken.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        )
        self._advapi32.OpenProcessToken.restype = wintypes.BOOL
        self._advapi32.GetTokenInformation.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.GetTokenInformation.restype = wintypes.BOOL
        self._advapi32.ConvertSidToStringSidW.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p),
        )
        self._advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        )
        self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self._advapi32.SetFileSecurityW.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
        )
        self._advapi32.SetFileSecurityW.restype = wintypes.BOOL
        self._kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def protect(self, path: Path) -> None:
        if _is_reparse_point(path):
            raise PortableConfigurationError("Local runtime path cannot be secured.")
        descriptor = ctypes.c_void_p()
        sddl = self._dacl_sddl()
        if not self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise PortableConfigurationError("Windows could not secure local runtime state.")
        try:
            if not self._advapi32.SetFileSecurityW(
                str(path),
                self._dacl_security_information | self._protected_dacl_security_information,
                descriptor,
            ):
                raise PortableConfigurationError("Windows could not secure local runtime state.")
        finally:
            self._kernel32.LocalFree(descriptor)

    def _dacl_sddl(self) -> str:
        # OI/CI makes subsequently-created files and directories inherit the DACL.
        sid = self._current_user_sid()
        return f"D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid})"

    def _current_user_sid(self) -> str:
        token = wintypes.HANDLE()
        if not self._advapi32.OpenProcessToken(
            self._kernel32.GetCurrentProcess(), self._token_query, ctypes.byref(token)
        ):
            raise PortableConfigurationError("Windows could not secure local runtime state.")
        try:
            required = wintypes.DWORD()
            self._advapi32.GetTokenInformation(
                token, self._token_user, None, 0, ctypes.byref(required)
            )
            if ctypes.get_last_error() != self._error_insufficient_buffer or not required.value:
                raise PortableConfigurationError("Windows could not secure local runtime state.")
            buffer = ctypes.create_string_buffer(required.value)
            if not self._advapi32.GetTokenInformation(
                token, self._token_user, ctypes.byref(buffer), required, ctypes.byref(required)
            ):
                raise PortableConfigurationError("Windows could not secure local runtime state.")
            sid = ctypes.c_void_p.from_buffer(buffer).value
            text = ctypes.c_wchar_p()
            if not sid or not self._advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise PortableConfigurationError("Windows could not secure local runtime state.")
            try:
                return text.value
            finally:
                self._kernel32.LocalFree(text)
        finally:
            self._kernel32.CloseHandle(token)


def secure_windows_path(path: Path, protector: WindowsAclProtector | None = None) -> None:
    """Fail closed when a Windows runtime path cannot be made private."""

    if os.name != "nt":
        return
    if not path.exists() or _is_reparse_point(path):
        raise PortableConfigurationError("Local runtime path cannot be secured.")
    (protector or WindowsAclProtector()).protect(path)


@dataclass(frozen=True, slots=True)
class PortableLayout:
    root: Path

    @property
    def app_dir(self) -> Path:
        return self.root / "app"

    @property
    def sidecar_dir(self) -> Path:
        return self.root / "sidecar"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gateway.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.resolve().as_posix()}"

    @property
    def sidecar_executable(self) -> Path:
        return self.sidecar_dir / "cli-proxy-api.exe"

    @property
    def sidecar_manifest(self) -> Path:
        return self.sidecar_dir / "manifest.json"

    @property
    def sidecar_config(self) -> Path:
        return self.config_dir / "cliproxy.local.yaml"

    @property
    def auth_dir(self) -> Path:
        return self.data_dir / "auths"

    @property
    def protected_secrets(self) -> Path:
        return self.data_dir / "runtime-secrets.dat"

    @property
    def shutdown_request(self) -> Path:
        return self.runtime_dir / "shutdown.request"

    @property
    def staging_dir(self) -> Path:
        return self.runtime_dir / "updates"

    @property
    def backup_dir(self) -> Path:
        return self.sidecar_dir / "backups"

    def ensure_directories(self) -> None:
        directories = (
            self.app_dir,
            self.sidecar_dir,
            self.data_dir,
            self.logs_dir,
            self.runtime_dir,
            self.config_dir,
            self.auth_dir,
            self.staging_dir,
            self.backup_dir,
        )
        for path in directories:
            path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt" and _is_reparse_point(self.root):
            raise PortableConfigurationError("Local runtime path cannot be secured.")
        for path in (
            self.data_dir,
            self.logs_dir,
            self.runtime_dir,
            self.config_dir,
            self.auth_dir,
            self.staging_dir,
        ):
            secure_windows_path(path)


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    sidecar_api_key: str
    management_key: str

    def __repr__(self) -> str:
        return "RuntimeSecrets(sidecar_api_key=<redacted>, management_key=<redacted>)"


class DataProtector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDataProtector:
    """Protect local runtime keys to the current Windows user with DPAPI."""

    _description = "Personal AI Provider Gateway local runtime keys"
    _ui_forbidden = 0x1

    def __init__(self) -> None:
        if os.name != "nt":
            raise PortableConfigurationError("Windows DPAPI is required for portable secrets.")
        self._crypt32 = ctypes.windll.crypt32
        self._kernel32 = ctypes.windll.kernel32

    def protect(self, value: bytes) -> bytes:
        return self._transform(value, protect=True)

    def unprotect(self, value: bytes) -> bytes:
        return self._transform(value, protect=False)

    def _transform(self, value: bytes, *, protect: bool) -> bytes:
        if not value:
            raise PortableConfigurationError("Protected local state is invalid.")
        buffer = ctypes.create_string_buffer(value)
        input_blob = _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = _DataBlob()
        if protect:
            succeeded = self._crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                self._description,
                None,
                None,
                None,
                self._ui_forbidden,
                ctypes.byref(output_blob),
            )
        else:
            succeeded = self._crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                self._ui_forbidden,
                ctypes.byref(output_blob),
            )
        if not succeeded:
            raise PortableConfigurationError("Windows could not unlock local runtime state.")
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            self._kernel32.LocalFree(output_blob.pbData)


class PortableSecretStore:
    def __init__(self, path: Path, protector: DataProtector) -> None:
        self._path = path
        self._protector = protector

    def load_or_create(self) -> RuntimeSecrets:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        secure_windows_path(self._path.parent)
        if self._path.exists():
            secure_windows_path(self._path)
            return self._load()
        created = RuntimeSecrets(
            sidecar_api_key=generate_secret(),
            management_key=generate_secret(),
        )
        encoded = self._protector.protect(
            json.dumps(
                {
                    "version": 1,
                    "sidecar_api_key": created.sidecar_api_key,
                    "management_key": created.management_key,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )
        try:
            with self._path.open("x", encoding="ascii") as protected_file:
                protected_file.write(base64.b64encode(encoded).decode("ascii"))
                protected_file.write("\n")
        except FileExistsError:
            secure_windows_path(self._path)
            return self._load()
        secure_windows_path(self._path)
        return created

    def _load(self) -> RuntimeSecrets:
        try:
            encrypted = base64.b64decode(
                self._path.read_text(encoding="ascii").strip(),
                validate=True,
            )
            payload = json.loads(self._protector.unprotect(encrypted).decode("utf-8"))
            if payload.get("version") != 1:
                raise ValueError
            secrets = RuntimeSecrets(
                sidecar_api_key=payload["sidecar_api_key"],
                management_key=payload["management_key"],
            )
        except Exception as error:
            if isinstance(error, PortableConfigurationError):
                raise
            raise PortableConfigurationError("Protected local runtime state is invalid.") from None
        values = (
            secrets.sidecar_api_key,
            secrets.management_key,
        )
        if any(not isinstance(value, str) or len(value) < 32 for value in values):
            raise PortableConfigurationError("Protected local runtime state is invalid.")
        if len(set(values)) != len(values):
            raise PortableConfigurationError("Local credential domains must be distinct.")
        return secrets


def resource_root() -> Path:
    """Return bundled data root or repository root without using the writable bundle."""

    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[2]


def validate_sidecar_install(
    layout: PortableLayout,
    verifier: Callable[[Path, str], bool] = verify_sha256,
) -> SidecarManifest:
    """Refuse a missing, untested, mismatched or path-escaped Sidecar pair."""

    executable = layout.sidecar_executable
    manifest_path = layout.sidecar_manifest
    try:
        if (
            executable.is_symlink()
            or manifest_path.is_symlink()
            or not executable.is_file()
            or not manifest_path.is_file()
            or executable.resolve().parent != layout.sidecar_dir.resolve()
            or manifest_path.resolve().parent != layout.sidecar_dir.resolve()
        ):
            raise PortableConfigurationError("The fixed Sidecar package is invalid.")
        manifest = SidecarManifest.load(manifest_path)
        if not manifest.tested or not verifier(executable, manifest.sha256):
            raise PortableConfigurationError("The fixed Sidecar package is invalid.")
        return manifest
    except PortableConfigurationError:
        raise
    except Exception:
        raise PortableConfigurationError("The fixed Sidecar package is invalid.") from None


def upgrade_database(database_url: str, resources: Path | None = None) -> None:
    root = (resources or resource_root()).resolve()
    ini = root / "alembic.ini"
    migrations = root / "migrations"
    if not ini.is_file() or not migrations.is_dir():
        raise PortableConfigurationError("Bundled database migrations are missing.")
    configuration = Config(str(ini))
    configuration.set_main_option("script_location", str(migrations))
    configuration.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    previous = os.environ.get("AIPG_DATABASE_URL")
    os.environ["AIPG_DATABASE_URL"] = database_url
    try:
        command.upgrade(configuration, "head")
    finally:
        if previous is None:
            os.environ.pop("AIPG_DATABASE_URL", None)
        else:
            os.environ["AIPG_DATABASE_URL"] = previous


def bootstrap_database(database_url: str) -> None:
    """Create a non-login initial admin verifier.

    Gateway API keys are created explicitly after administrator login so their
    full value can be delivered exactly once instead of being silently lost.
    """

    from .control.password_recovery import AdminPasswordStore

    engine = create_database_engine(database_url)
    password_store = AdminPasswordStore(engine)
    password_store.initialize_if_missing(hash_password(generate_secret()))


_MANAGEMENT_KEY_LINE = re.compile(r'^  secret-key: "([^"\r\n]+)"$', re.MULTILINE)
_SIDECAR_KEY_LINE = re.compile(r'^  - "([^"\r\n]+)"$', re.MULTILINE)
_SIDECAR_PROXY_LINE = re.compile(r'^proxy-url: "([^"\r\n]+)"\r?\n?', re.MULTILINE)


def _sidecar_config_content(
    layout: PortableLayout,
    sidecar_api_key: str,
    management_key: str,
    antigravity_proxy_url: str | None = None,
) -> str:
    auth_dir = layout.auth_dir.resolve().as_posix()
    proxy_line = (
        f'proxy-url: "{antigravity_proxy_url}"\n'
        if antigravity_proxy_url is not None
        else ""
    )
    return (
        'host: "127.0.0.1"\n'
        "port: 8317\n"
        "remote-management:\n"
        "  allow-remote: false\n"
        f'  secret-key: "{management_key}"\n'
        "  disable-control-panel: true\n"
        f'auth-dir: "{auth_dir}"\n'
        f"{proxy_line}"
        "api-keys:\n"
        f'  - "{sidecar_api_key}"\n'
        "debug: false\n"
        "logging-to-file: false\n"
        "usage-statistics-enabled: false\n"
        "routing:\n"
        '  strategy: "fill-first"\n'
        "  session-affinity: false\n"
    )


def _management_key_matches(configured: str, expected: str) -> bool:
    if hmac.compare_digest(configured.encode("utf-8"), expected.encode("utf-8")):
        return True
    if not configured.startswith(("$2a$", "$2b$", "$2y$")):
        return False
    try:
        return bcrypt.checkpw(expected.encode("utf-8"), configured.encode("ascii"))
    except (UnicodeEncodeError, ValueError):
        return False


def _sidecar_config_matches(
    content: str,
    layout: PortableLayout,
    runtime: RuntimeSecrets,
) -> bool:
    management = _MANAGEMENT_KEY_LINE.findall(content)
    sidecar = _SIDECAR_KEY_LINE.findall(content)
    proxies = _SIDECAR_PROXY_LINE.findall(content)
    if len(management) != 1 or len(sidecar) != 1 or len(proxies) > 1:
        return False
    if proxies:
        try:
            if normalize_antigravity_proxy_url(proxies[0]) != proxies[0]:
                return False
        except ValueError:
            return False
    skeleton = _MANAGEMENT_KEY_LINE.sub(
        '  secret-key: "<management-key>"', content
    )
    skeleton = _SIDECAR_KEY_LINE.sub('  - "<sidecar-api-key>"', skeleton)
    skeleton = _SIDECAR_PROXY_LINE.sub("", skeleton)
    expected = _sidecar_config_content(
        layout,
        "<sidecar-api-key>",
        "<management-key>",
    )
    return (
        hmac.compare_digest(skeleton.encode("utf-8"), expected.encode("utf-8"))
        and hmac.compare_digest(
            sidecar[0].encode("utf-8"), runtime.sidecar_api_key.encode("utf-8")
        )
        and _management_key_matches(management[0], runtime.management_key)
    )


def normalize_antigravity_proxy_url(value: str | None) -> str | None:
    """Accept only an unauthenticated local HTTP proxy endpoint."""

    if value is None or not value.strip():
        return None
    if not isinstance(value, str) or len(value) > 2048 or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("invalid Antigravity proxy URL")
    parts = urlsplit(value.strip())
    if (
        parts.scheme.lower() != "http"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or not parts.hostname
    ):
        raise ValueError("invalid Antigravity proxy URL")
    hostname = parts.hostname.lower()
    try:
        loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
        port = parts.port
    except ValueError as error:
        raise ValueError("invalid Antigravity proxy URL") from error
    if not loopback or port is None:
        raise ValueError("invalid Antigravity proxy URL")
    netloc = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
    return urlunsplit(("http", netloc, "", "", ""))


def antigravity_proxy_url(layout: PortableLayout, runtime: RuntimeSecrets) -> str | None:
    """Read the validated proxy setting without exposing it through an API response."""

    content = layout.sidecar_config.read_text(encoding="utf-8-sig")
    if not _sidecar_config_matches(content, layout, runtime):
        raise PortableConfigurationError(
            "Local Sidecar config does not match protected runtime state."
        )
    matches = _SIDECAR_PROXY_LINE.findall(content)
    return normalize_antigravity_proxy_url(matches[0]) if matches else None


def write_antigravity_proxy_url(
    layout: PortableLayout,
    runtime: RuntimeSecrets,
    value: str | None,
) -> bool:
    """Replace only the validated Sidecar proxy field, preserving all key checks."""

    normalized = normalize_antigravity_proxy_url(value)
    config_path = layout.sidecar_config
    if (
        not config_path.is_file()
        or config_path.is_symlink()
        or config_path.resolve().parent != layout.config_dir.resolve()
    ):
        raise PortableConfigurationError("Local Sidecar config is unavailable.")
    current = config_path.read_text(encoding="utf-8-sig")
    if not _sidecar_config_matches(current, layout, runtime):
        raise PortableConfigurationError(
            "Local Sidecar config does not match protected runtime state."
        )
    management = _MANAGEMENT_KEY_LINE.findall(current)
    replacement = _sidecar_config_content(
        layout,
        runtime.sidecar_api_key,
        management[0],
        normalized,
    )
    temporary = config_path.with_name(f".{config_path.name}.{generate_secret()}.tmp")
    with temporary.open("x", encoding="utf-8") as output:
        output.write(replacement)
    secure_windows_path(temporary)
    os.replace(temporary, config_path)
    secure_windows_path(config_path)
    return normalized is not None


def _hashed_management_key(value: str) -> str:
    encoded = bcrypt.hashpw(value.encode("utf-8"), bcrypt.gensalt(rounds=12))
    # CLIProxyAPI v7.2.153 writes the equivalent Go bcrypt $2a$ form.
    if encoded.startswith(b"$2b$"):
        encoded = b"$2a$" + encoded[4:]
    return encoded.decode("ascii")


def write_sidecar_config(layout: PortableLayout, runtime: RuntimeSecrets) -> None:
    config_path = layout.sidecar_config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    secure_windows_path(config_path.parent)
    if layout.sidecar_config.exists():
        secure_windows_path(config_path)
        if (
            config_path.is_symlink()
            or config_path.resolve().parent != layout.config_dir.resolve()
            or not _sidecar_config_matches(
                config_path.read_text(encoding="utf-8-sig"), layout, runtime
            )
        ):
            raise PortableConfigurationError(
                "Local Sidecar config does not match protected runtime state."
            )
        return
    content = _sidecar_config_content(
        layout,
        runtime.sidecar_api_key,
        _hashed_management_key(runtime.management_key),
    )
    try:
        with config_path.open("x", encoding="utf-8") as config_file:
            config_file.write(content)
    except FileExistsError:
        secure_windows_path(config_path)
        if (
            config_path.is_symlink()
            or config_path.resolve().parent != layout.config_dir.resolve()
            or not _sidecar_config_matches(
                config_path.read_text(encoding="utf-8-sig"), layout, runtime
            )
        ):
            raise PortableConfigurationError(
                "Local Sidecar config does not match protected runtime state."
            ) from None
    else:
        secure_windows_path(config_path)


def runtime_environment(layout: PortableLayout, runtime: RuntimeSecrets) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("AIPG_")
    }
    environment.update(
        {
            "AIPG_DATABASE_URL": layout.database_url,
            "AIPG_SIDECAR_EXECUTABLE": str(layout.sidecar_executable.resolve()),
            "AIPG_SIDECAR_CONFIG": str(layout.sidecar_config.resolve()),
            "AIPG_SIDECAR_BASE_URL": "http://127.0.0.1:8317",
            "AIPG_SIDECAR_API_KEY": runtime.sidecar_api_key,
            "AIPG_SIDECAR_MANAGEMENT_KEY": runtime.management_key,
            "AIPG_GATEWAY_BASE_URL": "http://127.0.0.1:8020/v1",
            "AIPG_PHASE6_ENABLED": "1",
            "AIPG_DASHBOARD_PASSWORD_BYPASS": "1",
            "AIPG_AUTOSTART_SIDECAR": "1",
            "AIPG_SHUTDOWN_REQUEST_PATH": str(layout.shutdown_request.resolve()),
            "AIPG_SIDECAR_MANIFEST": str(layout.sidecar_manifest.resolve()),
            "AIPG_SIDECAR_STAGING_ROOT": str(layout.staging_dir.resolve()),
            "AIPG_SIDECAR_BACKUP_ROOT": str(layout.backup_dir.resolve()),
        }
    )
    return environment
