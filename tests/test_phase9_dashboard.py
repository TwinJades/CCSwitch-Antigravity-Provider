import shutil
import subprocess

import pytest

from ai_provider_gateway.control.dashboard import DASHBOARD_HTML


def test_antigravity_connection_state_is_owned_by_health_not_model_catalogue():
    assert 'const renderAntigravityStatus = (health, models, customProviders)' in DASHBOARD_HTML
    assert 'const anyProviderConfigured = syncedCount > 0 || customProviders.length > 0' in DASHBOARD_HTML
    assert 'renderAntigravityStatus(healthResult.status === "fulfilled" ? healthResult.value : null, models, providers);' in DASHBOARD_HTML
    assert 'document.getElementById("provider-state").textContent = "\u5df2\u8fde\u63a5' not in DASHBOARD_HTML
    assert '\u5b9e\u65f6\u8fde\u63a5\u5f85\u786e\u8ba4\uff1b\u5df2\u6709 ' in DASHBOARD_HTML
    assert 'humanState(health.state) + "\uff1b\u5df2\u6709 " + syncedCount + " \u4e2a\u5386\u53f2\u540c\u6b65\u6a21\u578b"' in DASHBOARD_HTML
    assert '\u767b\u5f55\u6210\u529f\u4f46\u6a21\u578b\u540c\u6b65\u5f85\u5904\u7406' in DASHBOARD_HTML


def test_custom_provider_form_keeps_secrets_out_of_rendered_results():
    assert 'id="custom-provider-form"' in DASHBOARD_HTML
    assert 'id="custom-provider-api-key" type="password"' in DASHBOARD_HTML
    assert 'id="custom-provider-models"' in DASHBOARD_HTML
    assert 'id="custom-provider-model-fallback"' in DASHBOARD_HTML
    assert '连接失败时手动填写模型（可选）' in DASHBOARD_HTML
    assert 'Gateway 会先通过 /v1/models 自动同步' in DASHBOARD_HTML
    assert '\n  addDynamicRow("model");' not in DASHBOARD_HTML
    assert 'second.required = true' not in DASHBOARD_HTML
    assert 'customModelList.children.length === 1' not in DASHBOARD_HTML
    assert 'id="custom-provider-headers"' in DASHBOARD_HTML
    assert 'id="custom-provider-list"' in DASHBOARD_HTML
    assert 'const clearCustomProviderSecrets = ()' in DASHBOARD_HTML
    assert 'window.addEventListener("pagehide", clearCustomProviderSecrets);' in DASHBOARD_HTML
    assert 'clearCustomProviderSecrets();' in DASHBOARD_HTML
    assert 'customProviderForm.addEventListener("submit", async event =>' in DASHBOARD_HTML
    assert 'request("/api/control/providers/openai-compatible", payload)' in DASHBOARD_HTML
    assert 'readJson("/api/control/providers/openai-compatible")' in DASHBOARD_HTML
    assert 'provider.display_name + " · " + provider.provider_id + " · " + provider.model_count + " 个模型 · " + status' in DASHBOARD_HTML
    assert 'data.verification_status === "verified"' in DASHBOARD_HTML
    assert 'payload.api_key = null;' in DASHBOARD_HTML
    assert 'payload.headers.forEach(header => { header.value = ""; });' in DASHBOARD_HTML
    assert "localStorage" not in DASHBOARD_HTML
    assert "sessionStorage" not in DASHBOARD_HTML


def test_antigravity_proxy_ui_is_optional_sidecar_only_and_never_echoes_value():
    assert 'id="antigravity-proxy-url"' in DASHBOARD_HTML
    assert '代理地址（选填，仅用于 Antigravity）' in DASHBOARD_HTML
    assert 'request("/api/control/providers/antigravity/proxy"' in DASHBOARD_HTML
    assert 'proxy_url: null' in DASHBOARD_HTML
    assert 'status.configured' in DASHBOARD_HTML


def test_client_manual_fields_and_canonical_model_options_are_available():
    assert '\u624b\u5de5\u586b\u5199\u9879' in DASHBOARD_HTML
    assert 'personal-ai-gateway' in DASHBOARD_HTML
    assert 'http://127.0.0.1:8020/v1' in DASHBOARD_HTML
    assert 'currentConfig && currentConfig.baseUrl ? currentConfig.baseUrl' in DASHBOARD_HTML
    assert '\u5728\u5ba2\u6237\u7aef\u6b63\u5e38\u7684 API Key \u8f93\u5165\u6846\u7c98\u8d34 Gateway Key' in DASHBOARD_HTML
    assert '\u65e0\u9700\u586b\u5199' in DASHBOARD_HTML
    assert 'option.textContent = model.id;' in DASHBOARD_HTML
    assert 'copy.setAttribute("aria-label", "\u590d\u5236" + label);' in DASHBOARD_HTML


def test_provider_selection_and_key_management_are_inline_and_persistent():
    assert 'id="export-provider-form"' in DASHBOARD_HTML
    assert 'readJson("/api/control/integrations/export-providers")' in DASHBOARD_HTML
    assert 'request("/api/control/integrations/export-providers"' in DASHBOARD_HTML
    assert 'enabledProviders.has(model.provider)' in DASHBOARD_HTML
    assert 'readJson("/api/control/gateway-keys")' in DASHBOARD_HTML
    assert '.filter(item => item.status === "active")' in DASHBOARD_HTML
    assert '当前没有有效的 Gateway API Key。' in DASHBOARD_HTML
    assert '· 已删除' not in DASHBOARD_HTML
    assert 'remove.textContent = "\u5220\u9664 Key"' in DASHBOARD_HTML
    assert 'remove.textContent = "\u786e\u8ba4\u5220\u9664"' in DASHBOARD_HTML
    assert 'request("/api/control/gateway-keys/" + encodeURIComponent(item.key_id), undefined, "DELETE")' in DASHBOARD_HTML
    assert "window.confirm" not in DASHBOARD_HTML


def test_opencode_guide_recommends_global_config_and_uses_one_api_payload_for_all_exports():
    assert '%USERPROFILE%\\\\.config\\\\opencode\\\\opencode.json' in DASHBOARD_HTML
    assert "也可以改用 opencode.jsonc" in DASHBOARD_HTML
    assert "同一目录不要同时保留两份" in DASHBOARD_HTML
    assert 'id="download-config-jsonc"' in DASHBOARD_HTML
    assert 'downloadCurrentConfig("opencode.jsonc")' in DASHBOARD_HTML
    assert "项目级 opencode.json 或 opencode.jsonc" in DASHBOARD_HTML
    assert "可能覆盖全局的同名设置" in DASHBOARD_HTML
    assert 'configPreview.textContent = JSON.stringify(currentConfig, null, 2);' in DASHBOARD_HTML
    assert 'new Blob([JSON.stringify(currentConfig, null, 2) + "\\n"]' in DASHBOARD_HTML


def test_plaintext_opencode_key_is_injected_only_in_browser_memory_and_then_cleared():
    assert 'id="opencode-gateway-key" type="password"' in DASHBOARD_HTML
    assert 'const secret = kind === "opencode" ? (configGatewayKey.value || gatewayKey) : null;' in DASHBOARD_HTML
    assert 'currentConfig.provider["personal-ai-gateway"].options.apiKey = secret;' in DASHBOARD_HTML
    assert 'currentConfigContainsKey = true;' in DASHBOARD_HTML
    assert 'currentConfigKeyPrefix = secretPrefix;' in DASHBOARD_HTML
    assert 'if (item.prefix === currentConfigKeyPrefix) clearConfig();' in DASHBOARD_HTML
    assert 'clearGatewayKey();' in DASHBOARD_HTML
    assert 'window.addEventListener("pagehide", clearConfig);' in DASHBOARD_HTML
    assert "此配置包含明文 Gateway API Key，请勿上传、分享或提交到 Git。" in DASHBOARD_HTML
    assert "AIPG_GATEWAY_API_KEY" not in DASHBOARD_HTML
    assert "localStorage" not in DASHBOARD_HTML
    assert "sessionStorage" not in DASHBOARD_HTML


def test_dashboard_javascript_parses(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not available")
    script = DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    path = tmp_path / "dashboard.js"
    path.write_text(script, encoding="utf-8")
    subprocess.run(
        [node, "--check", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
