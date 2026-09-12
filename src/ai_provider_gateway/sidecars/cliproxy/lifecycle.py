"""Lifecycle ownership for a CLIProxyAPI process started by this application."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import subprocess
from typing import Protocol


class ProcessHandle(Protocol):
    """The small process surface needed by the lifecycle manager."""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[[Sequence[str], Path, int, subprocess.STARTUPINFO | None], ProcessHandle]
ReadinessProbe = Callable[[], Awaitable[bool]]
Sleeper = Callable[[float], Awaitable[None]]


class SidecarProcessState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    CRASHED = "crashed"


@dataclass(frozen=True, slots=True)
class SidecarLaunchSpec:
    """Explicit CLIProxyAPI command inputs; paths are never shell-concatenated."""

    executable: Path
    config_path: Path
    working_directory: Path | None = None

    @property
    def cwd(self) -> Path:
        return (self.working_directory or self.config_path.parent).resolve()

    @property
    def command(self) -> tuple[str, ...]:
        return (
            str(self.executable.resolve()),
            "-config",
            str(self.config_path.resolve()),
        )


@dataclass(frozen=True, slots=True)
class SidecarRuntimeState:
    state: SidecarProcessState
    pid: int | None
    exit_code: int | None = None


def _default_process_factory(
    command: Sequence[str],
    cwd: Path,
    creationflags: int,
    startupinfo: subprocess.STARTUPINFO | None,
) -> ProcessHandle:
    return subprocess.Popen(
        list(command),
        cwd=str(cwd),
        creationflags=creationflags,
        startupinfo=startupinfo,
        close_fds=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _windows_background_options() -> tuple[int, subprocess.STARTUPINFO | None]:
    """Avoid creating a visible console window when launched on Windows."""

    if not hasattr(subprocess, "STARTUPINFO"):
        return 0, None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return getattr(subprocess, "CREATE_NO_WINDOW", 0), startupinfo


class CLIProxyAPILifecycle:
    """Start and stop only the process handle created by this manager instance."""

    def __init__(
        self,
        launch_spec: SidecarLaunchSpec,
        *,
        process_factory: ProcessFactory = _default_process_factory,
        readiness_probe: ReadinessProbe | None = None,
        sleeper: Sleeper = asyncio.sleep,
        readiness_attempts: int = 1,
        readiness_interval: float = 0.2,
        stop_timeout: float = 5.0,
    ) -> None:
        if readiness_attempts < 1:
            raise ValueError("readiness_attempts must be at least one")
        self._launch_spec = launch_spec
        self._process_factory = process_factory
        self._readiness_probe = readiness_probe
        self._sleeper = sleeper
        self._readiness_attempts = readiness_attempts
        self._readiness_interval = readiness_interval
        self._stop_timeout = stop_timeout
        self._process: ProcessHandle | None = None
        self._mutation_lock = asyncio.Lock()

    async def start(self) -> SidecarRuntimeState:
        """Start once, or return the state of this manager's existing child process."""

        async with self._mutation_lock:
            return await self._start_unlocked()

    async def _start_unlocked(self) -> SidecarRuntimeState:
        state = await self.status()
        if state.state in {SidecarProcessState.RUNNING, SidecarProcessState.STARTING}:
            return state
        self._validate_launch_paths()
        creationflags, startupinfo = _windows_background_options()
        self._process = self._process_factory(
            self._launch_spec.command,
            self._launch_spec.cwd,
            creationflags,
            startupinfo,
        )
        return await self._await_readiness()

    async def stop(self) -> SidecarRuntimeState:
        """Stop the owned child only; never discover or terminate external processes."""

        async with self._mutation_lock:
            return await self._stop_unlocked()

    async def _stop_unlocked(self) -> SidecarRuntimeState:
        process = self._process
        if process is None:
            return SidecarRuntimeState(SidecarProcessState.STOPPED, None)
        if process.poll() is None:
            process.terminate()
            elapsed = 0.0
            while process.poll() is None and elapsed < self._stop_timeout:
                await self._sleeper(min(self._readiness_interval, self._stop_timeout - elapsed))
                elapsed += self._readiness_interval
            if process.poll() is None:
                process.kill()
        exit_code = process.poll()
        self._process = None
        return SidecarRuntimeState(SidecarProcessState.STOPPED, None, exit_code)

    async def restart(self) -> SidecarRuntimeState:
        """Restart the owned Sidecar process without touching any external process."""

        async with self._mutation_lock:
            await self._stop_unlocked()
            return await self._start_unlocked()

    async def status(self) -> SidecarRuntimeState:
        """Report only state observable from the owned child handle."""

        process = self._process
        if process is None:
            return SidecarRuntimeState(SidecarProcessState.STOPPED, None)
        exit_code = process.poll()
        if exit_code is not None:
            return SidecarRuntimeState(SidecarProcessState.CRASHED, process.pid, exit_code)
        if self._readiness_probe is not None and not await self._readiness_probe():
            return SidecarRuntimeState(SidecarProcessState.STARTING, process.pid)
        return SidecarRuntimeState(SidecarProcessState.RUNNING, process.pid)

    async def _await_readiness(self) -> SidecarRuntimeState:
        for attempt in range(self._readiness_attempts):
            state = await self.status()
            if state.state != SidecarProcessState.STARTING:
                return state
            if attempt + 1 < self._readiness_attempts:
                await self._sleeper(self._readiness_interval)
        return await self.status()

    def _validate_launch_paths(self) -> None:
        if not self._launch_spec.executable.is_file():
            raise FileNotFoundError("CLIProxyAPI executable was not found.")
        if not self._launch_spec.config_path.is_file():
            raise FileNotFoundError("CLIProxyAPI config file was not found.")
        if not self._launch_spec.cwd.is_dir():
            raise FileNotFoundError("CLIProxyAPI working directory was not found.")
