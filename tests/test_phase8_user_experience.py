from __future__ import annotations

from pathlib import Path

from ai_provider_gateway.control.dashboard import DASHBOARD_HTML
from ai_provider_gateway.launcher import PortableLauncher, startup_error_message
from ai_provider_gateway.portable import PortableConfigurationError, PortableLayout


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_URL = "http://127.0.0.1:8010/"
CLEAN_WINDOWS_NOTICE = (
    "本版本已在开发机器完成便携包核心流程验收，尚未在全新 Windows 环境验证。"
)


class _Process:
    _next_pid = 4000

    def __init__(self):
        self.pid = self._next_pid
        type(self)._next_pid += 1
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True

    def kill(self):
        self.stopped = True

    def wait(self, timeout=None):
        self.stopped = True
        return 0


def test_startup_errors_are_actionable_and_do_not_echo_raw_details():
    busy = startup_error_message(
        PortableConfigurationError("Supervisor cannot start because local port 8010 is in use.")
    )
    assert "8010、8020、8317" in busy
    assert "start.exe" in busy

    mismatch = startup_error_message(
        PortableConfigurationError("Local Sidecar config does not match protected runtime state.")
    )
    assert "没有覆盖任何内容" in mismatch
    assert "重新解压发行 ZIP" in mismatch

    raw = r"unexpected SECRET-VALUE at D:\private\private.db"
    generic = startup_error_message(RuntimeError(raw))
    assert "SECRET-VALUE" not in generic
    assert "private.db" not in generic
    assert "不要发送密码" in generic


def test_browser_open_failure_reports_dashboard_address(monkeypatch, tmp_path):
    layout = PortableLayout(tmp_path)
    layout.ensure_directories()
    processes = []
    notices = []

    def create_process(*_):
        process = _Process()
        processes.append(process)
        return process

    def request_shutdown(_):
        layout.shutdown_request.write_text("shutdown-requested\n", encoding="utf-8")

    monkeypatch.setattr(PortableLauncher, "_ensure_local_ports_available", lambda self: None)
    launcher = PortableLauncher(
        layout,
        {},
        process_factory=create_process,
        sleeper=request_shutdown,
        browser_open=lambda _: False,
        browser_failure_notify=notices.append,
        ready_probe=lambda _: True,
    )

    assert launcher.run() == 0
    assert notices == [DASHBOARD_URL]
    assert len(processes) == 2
    assert all(process.stopped for process in processes)


def test_dashboard_is_a_self_contained_first_use_guide_and_clears_keys():
    assert "<script src=" not in DASHBOARD_HTML
    assert "<link rel=" not in DASHBOARD_HTML
    assert "第一次使用或忘记密码？设置新密码" in DASHBOARD_HTML
    assert "连接 Antigravity" in DASHBOARD_HTML
    assert "创建一次性 Key" in DASHBOARD_HTML
    assert "生成含 Key 的 OpenCode 配置" in DASHBOARD_HTML
    assert "显示 Cline 填写项" in DASHBOARD_HTML
    assert "下载配置" in DASHBOARD_HTML
    assert "gatewayKey = null;" in DASHBOARD_HTML
    assert 'keyResult.textContent = "";' in DASHBOARD_HTML
    assert 'window.addEventListener("pagehide", clearGatewayKey);' in DASHBOARD_HTML
    assert "页面中的完整 Key 已清除，可以关闭此标签页" in DASHBOARD_HTML
    assert "JSON.stringify(currentConfig" in DASHBOARD_HTML
    assert "JSON.stringify(gatewayKey" not in DASHBOARD_HTML


def test_packaged_launcher_hides_tracebacks_and_keeps_service_failures_silent():
    entrypoint = (PROJECT_ROOT / "packaging" / "start.py").read_text(encoding="utf-8")
    spec = (PROJECT_ROOT / "packaging" / "start.spec").read_text(encoding="utf-8")
    assert "except Exception as error:" in entrypoint
    assert '"--service" not in sys.argv[1:]' in entrypoint
    assert "show_startup_error(error)" in entrypoint
    assert "disable_windowed_traceback=True" in spec


def test_root_readme_describes_migrated_baseline_without_claiming_release():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    opening = "\n".join(readme.splitlines()[:10])

    assert "暂无本项目 Release" in opening
    assert "releases/tag/" not in opening
    assert "https://github.com/TwinJades/Personal-AI-Provider-Gateway" in readme
    assert "旧项目的功能和验收结论不自动适用于本项目" in readme
    assert ".venv\\Scripts\\python.exe -m pytest" not in readme
