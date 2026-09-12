"""Windows portable start.exe orchestration for owned local services."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Callable, Protocol, Sequence
from urllib.request import ProxyHandler, build_opener
import uuid
import webbrowser

from .portable import (
    PortableConfigurationError,
    PortableLayout,
    PortableSecretStore,
    WindowsDataProtector,
    bootstrap_database,
    resource_root,
    runtime_environment,
    upgrade_database,
    validate_sidecar_install,
    write_sidecar_config,
)
from .logging_config import configure_logging


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


ProcessFactory = Callable[[Sequence[str], Path, dict[str, str]], ProcessHandle]
ReadyProbe = Callable[[str], bool]
UserNotifier = Callable[[str], None]


_LOCAL_SERVICE_PORTS = (
    ("Supervisor", 8010),
    ("Gateway", 8020),
    ("CLIProxyAPI Sidecar", 8317),
)
_DASHBOARD_URL = "http://127.0.0.1:8010/"


def _show_windows_message(title: str, message: str, icon: int) -> None:
    """Show a small user-facing message without exposing exception details."""

    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000000 | icon)
        return
    print(f"{title}: {message}", file=sys.stderr)


def show_dashboard_address(url: str = _DASHBOARD_URL) -> None:
    _show_windows_message(
        "AI Provider Gateway 已启动",
        "本地网关已启动，但浏览器没有自动打开。\n\n"
        f"请在浏览器中打开：\n{url}",
        0x00000040,
    )


def startup_error_message(error: Exception) -> str:
    """Map internal startup errors to stable, non-sensitive recovery guidance."""

    detail = str(error).casefold()
    if "local port" in detail and "in use" in detail:
        return (
            "本地网关无法启动，因为所需端口已被占用。\n\n"
            "请关闭另一个 AI Provider Gateway，或结束占用 8010、8020、8317 "
            "端口的程序，然后再次双击 start.exe。"
        )
    if "does not match protected runtime state" in detail:
        return (
            "本地配置与当前 Windows 用户保护的运行状态不匹配；程序没有覆盖任何内容。\n\n"
            "如果你没有把目录移到其他 Windows 账户，请从备份恢复该目录；"
            "否则请重新解压发行 ZIP。"
        )
    if "another launcher" in detail:
        return (
            "这个目录正在启动或退出。\n\n请等待几秒，然后再次双击 start.exe。"
        )
    if "sidecar" in detail or "manifest" in detail:
        return (
            "内置 Provider 服务缺失或未通过完整性检查。\n\n"
            "请重新下载发行 ZIP，完整解压所有文件后再启动。"
        )
    return (
        "本地网关无法安全启动。\n\n请尝试重新解压发行 ZIP。若问题仍然存在，"
        "反馈时不要发送密码、API Key、OAuth Credential、数据库或原始日志。"
    )


def show_startup_error(error: Exception) -> None:
    _show_windows_message(
        "AI Provider Gateway 无法启动",
        startup_error_message(error),
        0x00000010,
    )


@contextmanager
def _portable_launch_lock(root: Path, timeout_ms: int = 1_500):
    """Serialize launch/shutdown for one portable directory on Windows."""

    if os.name != "nt":
        yield
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    digest = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()
    handle = kernel32.CreateMutexW(None, False, f"Local\\AIPG-{digest}")
    if not handle:
        raise PortableConfigurationError("Could not create the portable launch lock.")
    acquired = False
    try:
        result = kernel32.WaitForSingleObject(handle, timeout_ms)
        acquired = result in (0x00000000, 0x00000080)
        if not acquired:
            raise PortableConfigurationError(
                "Another launcher for this portable directory is still running."
            )
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def _background_process(
    command: Sequence[str],
    cwd: Path,
    environment: dict[str, str],
) -> ProcessHandle:
    startupinfo = None
    creationflags = 0
    if hasattr(subprocess, "STARTUPINFO"):
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def _terminate_owned_process_tree(process: ProcessHandle) -> None:
    """Stop the exact launcher-owned process tree on Windows."""

    if os.name == "nt" and isinstance(process, subprocess.Popen):
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return
    process.terminate()


def _http_ready(url: str, expected_service: str) -> bool:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url, timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return (
            response.status == 200
            and payload.get("status") == "ok"
            and payload.get("service") == expected_service
        )
    except Exception:
        return False


@dataclass(slots=True)
class PortableLauncher:
    layout: PortableLayout
    environment: dict[str, str]
    process_factory: ProcessFactory = _background_process
    sleeper: Callable[[float], None] = time.sleep
    browser_open: Callable[[str], object] = webbrowser.open
    browser_failure_notify: UserNotifier = show_dashboard_address
    # Test callers may inject a lightweight probe. Production always uses the
    # identity-aware HTTP probe below.
    ready_probe: ReadyProbe | None = None
    ready_attempts: int = 60
    ready_interval: float = 0.25

    def run(self) -> int:
        self._ensure_local_ports_available()
        self._archive_shutdown_marker()
        supervisor: ProcessHandle | None = None
        gateway: ProcessHandle | None = None
        try:
            supervisor = self.process_factory(
                self._service_command("supervisor"),
                self.layout.root,
                self._service_environment("supervisor"),
            )
            self._wait_ready(
                "http://127.0.0.1:8010/healthz", "supervisor", supervisor
            )
            gateway = self.process_factory(
                self._service_command("gateway"),
                self.layout.root,
                self._service_environment("gateway"),
            )
            self._wait_ready("http://127.0.0.1:8020/healthz", "gateway", gateway)
            opened = self.browser_open(_DASHBOARD_URL)
            if opened is False:
                self.browser_failure_notify(_DASHBOARD_URL)
            while True:
                if self.layout.shutdown_request.exists():
                    return 0
                if supervisor.poll() is not None:
                    raise PortableConfigurationError("Supervisor exited unexpectedly.")
                if gateway.poll() is not None:
                    gateway = self.process_factory(
                        self._service_command("gateway"),
                        self.layout.root,
                        self._service_environment("gateway"),
                    )
                    self._wait_ready(
                        "http://127.0.0.1:8020/healthz", "gateway", gateway
                    )
                self.sleeper(0.5)
        except KeyboardInterrupt:
            return 0
        finally:
            self._stop_owned(gateway)
            self._stop_owned(supervisor)
            self._archive_shutdown_marker()

    def _service_command(self, service: str) -> tuple[str, ...]:
        if getattr(sys, "frozen", False):
            return (str(Path(sys.executable).resolve()), "--service", service)
        return (
            str(Path(sys.executable).resolve()),
            "-m",
            "ai_provider_gateway.launcher",
            "--service",
            service,
        )

    def _service_environment(self, service: str) -> dict[str, str]:
        environment = {
            key: value
            for key, value in self.environment.items()
            if not key.startswith("AIPG_")
        }
        if service == "gateway":
            allowed = (
                "AIPG_DATABASE_URL",
                "AIPG_SIDECAR_BASE_URL",
                "AIPG_SIDECAR_API_KEY",
            )
        elif service == "supervisor":
            allowed = tuple(
                key
                for key in self.environment
                if key.startswith("AIPG_") and key != "AIPG_GATEWAY_API_KEY"
            )
        else:
            raise PortableConfigurationError("Unknown local service.")
        environment.update(
            {key: self.environment[key] for key in allowed if key in self.environment}
        )
        return environment

    @staticmethod
    def _ensure_local_ports_available() -> None:
        """Fail closed before creating children when a fixed loopback port is busy.

        The bound sockets are never put into listen mode and are closed before
        launch; they prove only that all required addresses were available at
        one instant, not that another local process cannot race the launch.
        """

        probes: list[socket.socket] = []
        try:
            for service, port in _LOCAL_SERVICE_PORTS:
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    probe.setsockopt(
                        socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1
                    )
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as error:
                    probe.close()
                    raise PortableConfigurationError(
                        f"{service} cannot start because local port {port} is in use."
                    ) from error
                probes.append(probe)
        finally:
            for probe in probes:
                probe.close()

    def _wait_ready(
        self, url: str, expected_service: str, process: ProcessHandle
    ) -> None:
        for _ in range(self.ready_attempts):
            if process.poll() is not None:
                raise PortableConfigurationError("A local service exited during startup.")
            ready = (
                _http_ready(url, expected_service)
                if self.ready_probe is None
                else self.ready_probe(url)
            )
            if ready:
                return
            self.sleeper(self.ready_interval)
        raise PortableConfigurationError("A local service did not become ready.")

    def _stop_owned(self, process: ProcessHandle | None) -> None:
        if process is None or process.poll() is not None:
            return
        _terminate_owned_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _archive_shutdown_marker(self) -> None:
        marker = self.layout.shutdown_request
        if not marker.exists():
            return
        history = self.layout.runtime_dir / "shutdown-history"
        history.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        marker.replace(history / f"{stamp}-{uuid.uuid4().hex}.request")


def _portable_root(argument: str | None) -> Path:
    if argument:
        return Path(argument).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def _serve(service: str) -> int:
    import uvicorn

    configure_logging()

    if service == "supervisor":
        from .control.runtime_app import create_runtime_app

        application = create_runtime_app()
        port = 8010
    elif service == "gateway":
        from .gateway.runtime_app import create_runtime_app

        application = create_runtime_app()
        port = 8020
    else:
        raise PortableConfigurationError("Unknown local service.")
    uvicorn.run(application, host="127.0.0.1", port=port, log_config=None)
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Start the local AI Provider Gateway.")
    parser.add_argument("--service", choices=("supervisor", "gateway"))
    parser.add_argument("--root")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start services without opening the Dashboard browser.",
    )
    options = parser.parse_args(arguments)
    if options.service:
        return _serve(options.service)

    layout = PortableLayout(_portable_root(options.root))
    layout.ensure_directories()
    try:
        with _portable_launch_lock(layout.root):
            validate_sidecar_install(layout)
            runtime = PortableSecretStore(
                layout.protected_secrets,
                WindowsDataProtector(),
            ).load_or_create()
            write_sidecar_config(layout, runtime)
            upgrade_database(layout.database_url, resource_root())
            bootstrap_database(layout.database_url)
            return PortableLauncher(
                layout,
                runtime_environment(layout, runtime),
                browser_open=(lambda _: None) if options.no_browser else webbrowser.open,
            ).run()
    except PortableConfigurationError as error:
        if "Another launcher" in str(error) and _http_ready(
            "http://127.0.0.1:8010/healthz", "supervisor"
        ):
            if not options.no_browser:
                opened = webbrowser.open(_DASHBOARD_URL)
                if opened is False:
                    show_dashboard_address(_DASHBOARD_URL)
            return 0
        raise


if __name__ == "__main__":
    raise SystemExit(main())
