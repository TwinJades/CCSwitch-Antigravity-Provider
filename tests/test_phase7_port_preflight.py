from __future__ import annotations

import socket

import pytest

from ai_provider_gateway import launcher as launcher_module
from ai_provider_gateway.launcher import PortableLauncher, _http_ready
from ai_provider_gateway.portable import PortableConfigurationError, PortableLayout


class _Process:
    pid = 1

    def poll(self):
        return None

    def terminate(self):
        return None

    def kill(self):
        return None

    def wait(self, timeout=None):
        return 0


def test_busy_fixed_port_fails_before_any_child_process_starts(tmp_path):
    layout = PortableLayout(tmp_path)
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 8010))
    created = []
    try:
        runner = PortableLauncher(
            layout,
            {},
            process_factory=lambda *args: created.append(args),
            ready_probe=lambda _: True,
        )
        with pytest.raises(PortableConfigurationError, match="Supervisor.*8010.*in use"):
            runner.run()
    finally:
        occupied.close()
    assert created == []


def test_port_preflight_leaves_no_listener_behind():
    PortableLauncher._ensure_local_ports_available()
    probes = []
    try:
        for _, port in launcher_module._LOCAL_SERVICE_PORTS:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.bind(("127.0.0.1", port))
            probes.append(probe)
    finally:
        for probe in probes:
            probe.close()


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _Opener:
    def __init__(self, payload):
        self.payload = payload

    def open(self, url, timeout):
        return _Response(self.payload)


def test_http_readiness_requires_the_expected_service_identity(monkeypatch):
    monkeypatch.setattr(
        launcher_module,
        "build_opener",
        lambda *_: _Opener(b'{"status":"ok","service":"supervisor"}'),
    )
    assert _http_ready("http://127.0.0.1:8010/healthz", "supervisor")
    assert not _http_ready("http://127.0.0.1:8020/healthz", "gateway")


def test_wait_ready_passes_expected_service_to_production_probe(monkeypatch, tmp_path):
    layout = PortableLayout(tmp_path)
    seen = []
    monkeypatch.setattr(
        launcher_module,
        "_http_ready",
        lambda url, service: seen.append((url, service)) or True,
    )
    runner = PortableLauncher(layout, {}, sleeper=lambda _: None)
    runner._wait_ready("http://127.0.0.1:8020/healthz", "gateway", _Process())
    assert seen == [("http://127.0.0.1:8020/healthz", "gateway")]
