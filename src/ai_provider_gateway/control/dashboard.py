"""Dependency-free, user-facing local Dashboard for the portable application."""

from __future__ import annotations


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Provider Gateway</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
      --ink: #152033; --muted: #607086; --line: #dbe4ef; --panel: #ffffff;
      --canvas: #f4f7fb; --blue: #1768e5; --blue-dark: #0e4fb7;
      --green: #16845b; --amber: #a56400; --red: #c33737; --soft-blue: #edf5ff;
      --shadow: 0 18px 48px rgba(35, 56, 86, .11);
    }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; background: var(--canvas); color: var(--ink); }
    button, input, select { font: inherit; }
    button { cursor: pointer; }
    button:disabled { cursor: not-allowed; opacity: .55; }
    [hidden] { display: none !important; }
    .topbar { background: #10203a; color: white; }
    .topbar-inner { width: min(1160px, calc(100% - 32px)); margin: auto; min-height: 68px; display: flex; align-items: center; justify-content: space-between; gap: 18px; }
    .brand { display: flex; align-items: center; gap: 12px; font-weight: 750; }
    .logo { width: 34px; height: 34px; display: grid; place-items: center; border-radius: 10px; background: linear-gradient(135deg, #4fa1ff, #7d6cff); box-shadow: inset 0 0 0 1px rgba(255,255,255,.22); }
    .privacy { color: #bdcbe0; font-size: .9rem; }
    .shell { width: min(1160px, calc(100% - 32px)); margin: 34px auto 64px; }
    .hero { display: grid; grid-template-columns: 1.25fr .75fr; gap: 22px; align-items: stretch; }
    .hero-copy, .auth-card, .card { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); }
    .hero-copy { padding: 36px; background: radial-gradient(circle at 92% 8%, #ddecff 0, transparent 40%), white; }
    .eyebrow { margin: 0 0 10px; color: var(--blue); font-size: .78rem; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }
    h1 { margin: 0; max-width: 720px; font-size: clamp(1.85rem, 4vw, 3rem); line-height: 1.12; letter-spacing: -.035em; }
    .lede { margin: 16px 0 0; max-width: 700px; color: var(--muted); font-size: 1.03rem; line-height: 1.75; }
    .trust { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 22px; }
    .tag { padding: 7px 10px; border-radius: 999px; color: #34506f; background: #eef4fb; font-size: .82rem; }
    .auth-card { padding: 27px; }
    .auth-card h2, .card h2 { margin: 0 0 8px; font-size: 1.12rem; }
    .subtle { margin: 0; color: var(--muted); line-height: 1.6; font-size: .92rem; }
    form { display: grid; gap: 11px; margin-top: 18px; }
    label { color: #405168; font-size: .83rem; font-weight: 700; }
    input, select { width: 100%; padding: 11px 12px; color: var(--ink); background: white; border: 1px solid #bdcada; border-radius: 10px; outline: none; }
    input:focus, select:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(23,104,229,.12); }
    .actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 16px; }
    .button { min-height: 42px; padding: 9px 15px; border: 1px solid #bdcada; border-radius: 10px; color: #30435c; background: white; font-weight: 700; }
    .button:hover { background: #f6f9fd; }
    .button.primary { color: white; background: var(--blue); border-color: var(--blue); }
    .button.primary:hover { background: var(--blue-dark); }
    .button.danger { color: #a02f2f; }
    .button.link { min-height: 0; padding: 0; border: 0; color: var(--blue); background: transparent; font-weight: 700; }
    .message { margin: 22px 0 0; padding: 13px 15px; border: 1px solid #cfe0f4; border-radius: 12px; color: #36516f; background: var(--soft-blue); line-height: 1.55; }
    .message[data-tone="success"] { color: #116443; border-color: #bee7d7; background: #eefaf5; }
    .message[data-tone="warning"] { color: #7a4d08; border-color: #f1d5a8; background: #fff8e9; }
    .message[data-tone="error"] { color: #8e2d2d; border-color: #efc7c7; background: #fff1f1; }
    .workspace { margin-top: 24px; }
    .steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 0 0 18px; padding: 0; list-style: none; }
    .step { min-height: 74px; padding: 14px; border: 1px solid var(--line); border-radius: 13px; background: white; }
    .step small { display: block; margin-bottom: 4px; color: var(--muted); }
    .step strong { font-size: .91rem; }
    .step[data-state="complete"] { border-color: #a8dec9; background: #f0faf6; }
    .step[data-state="active"] { border-color: #91bff9; background: #f1f7ff; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 18px; }
    .card { grid-column: span 6; padding: 24px; box-shadow: 0 10px 30px rgba(35,56,86,.07); }
    .card.wide { grid-column: 1 / -1; }
    .status-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 16px; }
    .metric { padding: 13px; border: 1px solid var(--line); border-radius: 12px; background: #f9fbfe; }
    .metric span { display: block; color: var(--muted); font-size: .76rem; }
    .metric strong { display: block; margin-top: 4px; font-size: .98rem; overflow-wrap: anywhere; }
    .status-line { display: flex; align-items: center; gap: 8px; margin-top: 14px; color: var(--muted); }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: #a5b1c0; }
    .dot.ok { background: var(--green); }
    .dot.warn { background: var(--amber); }
    .key-result { margin-top: 15px; padding: 15px; border: 1px solid #9ed7c1; border-radius: 12px; background: #effaf6; }
    .key-result code { display: block; margin: 9px 0; padding: 10px; border-radius: 8px; color: #163c31; background: white; overflow-wrap: anywhere; user-select: all; }
    .warning { color: #7a4d08; font-size: .86rem; line-height: 1.55; }
    .model-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-top: 16px; }
    .form-stack { display: grid; gap: 12px; margin-top: 18px; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .dynamic-list { display: grid; gap: 10px; }
    .dynamic-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; gap: 8px; align-items: end; }
    .dynamic-row .button { min-width: 42px; padding: 9px; }
    .field-note { margin: -4px 0 0; color: var(--muted); font-size: .82rem; line-height: 1.45; }
    .manual-fields { display: grid; gap: 9px; margin-top: 18px; }
    .manual-field { display: grid; grid-template-columns: 160px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #f9fbfe; }
    .manual-field dt { color: var(--muted); font-size: .84rem; }
    .manual-field dd { margin: 0; overflow-wrap: anywhere; }
    .provider-summary { display: grid; gap: 8px; margin-top: 16px; }
    .provider-summary p { margin: 0; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #f9fbfe; }
    .key-list, .provider-selection { display: grid; gap: 10px; margin-top: 16px; }
    .key-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #f9fbfe; }
    .key-item span { min-width: 0; overflow-wrap: anywhere; }
    .key-item .button { white-space: nowrap; }
    .key-item p { margin: 0; color: var(--muted); font-size: .84rem; overflow-wrap: anywhere; }
    .provider-choice { display: flex; gap: 9px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; background: #f9fbfe; }
    .provider-choice input { width: auto; }
    .secret-config { margin-top: 16px; padding: 14px; border: 1px solid #f1c36d; border-radius: 10px; background: #fff8e9; }
    .config-tools { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 12px; }
    pre { max-height: 290px; margin: 14px 0 0; padding: 14px; overflow: auto; color: #dce9f8; background: #112038; border-radius: 12px; font: .8rem/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
    .guide { display: grid; gap: 9px; margin: 16px 0 0; padding-left: 20px; color: #405168; line-height: 1.55; }
    .footer-actions { display: flex; justify-content: space-between; align-items: center; gap: 14px; margin-top: 20px; padding: 18px 2px; }
    .footer-actions p { margin: 0; color: var(--muted); font-size: .85rem; }
    @media (max-width: 820px) {
      .hero { grid-template-columns: 1fr; }
      .steps { grid-template-columns: repeat(2, 1fr); }
      .card { grid-column: 1 / -1; }
      .status-grid { grid-template-columns: repeat(2, 1fr); }
    }
    @media (max-width: 520px) {
      .topbar-inner, .shell { width: min(100% - 20px, 1160px); }
      .privacy { display: none; }
      .hero-copy, .auth-card, .card { padding: 20px; border-radius: 14px; }
      .steps, .status-grid { grid-template-columns: 1fr; }
      .model-row { grid-template-columns: 1fr; }
      .form-grid, .dynamic-row, .manual-field, .key-item { grid-template-columns: 1fr; }
      .dynamic-row .button, .manual-field .button { width: fit-content; }
      .footer-actions { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body data-control-mode="password">
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand"><span class="logo" aria-hidden="true">AI</span><span>Personal AI Provider Gateway</span></div>
      <div class="privacy">仅在这台电脑运行 · localhost</div>
    </div>
  </header>

  <main class="shell">
    <div class="hero">
      <section class="hero-copy">
        <p class="eyebrow">Windows 本地 AI 网关</p>
        <h1>把你的 Provider 安全连接到 OpenCode 或 Cline</h1>
        <p class="lede">按照页面步骤完成登录、创建本地访问 Key 和客户端配置。无需安装 Python，也不需要理解内部服务。</p>
        <div class="trust" aria-label="产品特性">
          <span class="tag">数据保存在本机</span><span class="tag">兼容 OpenAI API</span><span class="tag">可随时完整退出</span>
        </div>
        <div id="message" class="message" role="status" aria-live="polite">正在打开本机管理页面…</div>
      </section>

      <section class="auth-card" id="auth-card">
        <section id="login">
          <p class="eyebrow">管理入口</p>
          <h2>登录本机 Gateway</h2>
          <p class="subtle">密码只用于管理这台电脑上的服务。</p>
          <form id="login-form">
            <label for="password">管理密码</label>
            <input id="password" type="password" autocomplete="current-password" placeholder="输入管理密码" required>
            <button class="button primary" type="submit">登录并继续</button>
          </form>
          <div class="actions"><button class="button link" id="start-recovery" type="button">第一次使用或忘记密码？设置新密码</button></div>
        </section>
        <section id="recovery" hidden>
          <p class="eyebrow">首次设置 / 密码恢复</p>
          <h2>设置管理密码</h2>
          <p class="subtle">先在 Windows 原生确认框中允许本次操作，再设置至少 12 个字符的新密码。</p>
          <form id="recovery-form">
            <label for="new-password">新管理密码</label>
            <input id="new-password" type="password" autocomplete="new-password" minlength="12" maxlength="1024" placeholder="至少 12 个字符" required>
            <button class="button primary" type="submit">保存新密码</button>
          </form>
          <div class="actions"><button class="button link" id="cancel-recovery" type="button">返回登录</button></div>
        </section>
      </section>
    </div>

    <section id="signed-in" class="workspace" hidden>
      <ol class="steps" aria-label="首次使用进度">
        <li class="step" id="step-signin" data-state="complete"><small>步骤 1</small><strong>已登录管理页面</strong></li>
        <li class="step" id="step-provider" data-state="active"><small>步骤 2</small><strong>连接 Provider</strong></li>
        <li class="step" id="step-key"><small>步骤 3</small><strong>创建客户端 Key</strong></li>
        <li class="step" id="step-client"><small>步骤 4</small><strong>配置并测试客户端</strong></li>
      </ol>

      <div class="grid">
        <section class="card wide">
          <p class="eyebrow">运行状态</p>
          <h2>这台电脑上的服务</h2>
          <p class="subtle">状态、模型、额度和使用量互相独立；额度无法读取时会显示“未知”，不会影响请求。</p>
          <div class="status-grid">
            <div class="metric"><span>本地服务</span><strong id="sidecar-state">检查中…</strong></div>
            <div class="metric"><span>可用模型</span><strong id="model-count">—</strong></div>
            <div class="metric"><span>Provider 额度</span><strong id="quota-state">—</strong></div>
            <div class="metric"><span>今日请求</span><strong id="usage-count">—</strong></div>
          </div>
          <div class="actions"><button class="button" id="refresh-dashboard" type="button">刷新状态</button></div>
        </section>

        <section class="card">
          <p class="eyebrow">步骤 2</p>
          <h2>连接 Antigravity</h2>
          <p class="subtle">点击后会打开 Provider 官方登录页面。本项目不会接收你的 Google 密码，也不会读取 OAuth Credential。</p>
          <div class="status-line"><span class="dot" id="provider-dot"></span><span id="provider-state">尚未检查</span></div>
          <div class="actions">
            <button class="button primary" id="connect-provider" type="button">打开 Provider 登录</button>
            <button class="button" id="discover-models" type="button">登录完成，刷新模型</button>
          </div>
          <form id="antigravity-proxy-form">
            <label for="antigravity-proxy-url">代理地址（选填，仅用于 Antigravity）</label>
            <input id="antigravity-proxy-url" type="url" inputmode="url" autocomplete="off" maxlength="2048" placeholder="http://127.0.0.1:端口">
            <p class="field-note" id="antigravity-proxy-state">留空时直接连接；仅支持不含用户名和密码的本地 HTTP 代理。</p>
            <div class="actions">
              <button class="button" id="save-antigravity-proxy" type="submit">保存代理</button>
              <button class="button danger" id="clear-antigravity-proxy" type="button">清除代理</button>
            </div>
          </form>
        </section>

        <section class="card wide">
          <p class="eyebrow">自定义 Provider</p>
          <h2>导入学校或其他 OpenAI-compatible Provider</h2>
          <p class="subtle">仅导入与 OpenAI API 兼容的服务。API Key 与请求头值只用于本次提交，提交后会立即从页面清除。</p>
          <form id="custom-provider-form" class="form-stack">
            <div class="form-grid">
              <div><label for="custom-provider-id">Provider ID</label><input id="custom-provider-id" type="text" inputmode="latin" autocomplete="off" pattern="[a-z0-9_-]+" maxlength="128" placeholder="school-api" required></div>
              <div><label for="custom-provider-name">显示名称</label><input id="custom-provider-name" type="text" maxlength="128" placeholder="学校 AI Provider" required></div>
            </div>
            <div><label for="custom-provider-base-url">Base URL</label><input id="custom-provider-base-url" type="url" inputmode="url" autocomplete="url" maxlength="2048" placeholder="https://api.example.edu/v1" required></div>
            <div><label for="custom-provider-api-key">API Key（选填）</label><input id="custom-provider-api-key" type="password" autocomplete="off" maxlength="4096" placeholder="如由请求头认证可留空"><p class="field-note">不会在导入成功后的列表、配置预览或浏览器存储中显示；再次保存同一 Provider 时留空会保留已保存的 Key。</p></div>
            <details id="custom-provider-model-fallback">
              <summary>连接失败时手动填写模型（可选）</summary>
              <p class="field-note">通常无需填写；Gateway 会先通过 /v1/models 自动同步。只有 Provider 不支持模型列表接口或连接暂时失败时，才在这里添加备用模型。</p>
              <div id="custom-provider-models" class="dynamic-list" aria-label="备用模型列表"></div>
              <div class="actions"><button class="button" id="add-custom-model" type="button">添加备用模型</button></div>
            </details>
            <div>
              <label>请求头（选填）</label>
              <div id="custom-provider-headers" class="dynamic-list" aria-label="请求头列表"></div>
              <div class="actions"><button class="button" id="add-custom-header" type="button">添加请求头</button></div>
            </div>
            <div class="actions"><button class="button primary" id="submit-custom-provider" type="submit">导入 Provider</button></div>
          </form>
          <h3>已导入 Provider</h3>
          <div id="custom-provider-list" class="provider-summary"><p class="subtle">尚未导入自定义 Provider。</p></div>
        </section>

        <section class="card">
          <p class="eyebrow">步骤 3</p>
          <h2>创建客户端访问 Key</h2>
          <p class="subtle">这个 Key 供 OpenCode 或 Cline 访问本机 Gateway，与管理密码相互独立。</p>
          <form id="key-form">
            <label for="key-label">客户端名称（必须唯一）</label>
            <input id="key-label" type="text" maxlength="64" value="my-computer" placeholder="例如：my-opencode" required>
            <button class="button primary" type="submit">创建一次性 Key</button>
          </form>
          <div id="key-result" class="key-result" hidden></div>
          <p class="warning">完整 Key 只显示一次。请先保存，再进入下一步；退出或关闭页面后无法重新查看。</p>
          <h3>已有 Gateway Key</h3>
          <div id="gateway-key-list" class="key-list"><p class="subtle">正在读取 Key 列表…</p></div>
        </section>

        <section class="card wide">
          <p class="eyebrow">步骤 4</p>
          <h2>配置你的客户端</h2>
          <p class="subtle">先选择要导出的 Provider 和模型。OpenCode、Cline 或其他客户端都可以在正常的 API Key 输入位置直接填写 Gateway Key，不要求 Windows 环境变量。</p>
          <form id="export-provider-form">
            <h3>导出哪些 Provider</h3>
            <p class="subtle">默认全部启用；这里只过滤客户端配置，不会删除、断开或改变 Gateway 路由。</p>
            <div id="export-provider-list" class="provider-selection"><p class="subtle">正在读取 Provider…</p></div>
            <div class="actions"><button class="button" id="save-export-providers" type="submit">保存导出选择</button></div>
          </form>
          <div class="model-row">
            <select id="model-select" aria-label="选择模型"><option value="">请先连接 Provider 并刷新模型</option></select>
            <button class="button" id="reload-models" type="button">刷新模型列表</button>
          </div>
          <div class="secret-config">
            <label for="opencode-gateway-key">用于完整 OpenCode 配置的 Gateway API Key</label>
            <input id="opencode-gateway-key" type="password" autocomplete="off" maxlength="512" placeholder="可粘贴 Key；刚创建的 Key 也会自动使用">
            <p class="warning">此配置包含明文 Gateway API Key，请勿上传、分享或提交到 Git。</p>
          </div>
          <div class="config-tools">
            <button class="button primary" id="show-opencode" type="button">生成含 Key 的 OpenCode 配置</button>
            <button class="button primary" id="show-cline" type="button">显示 Cline 填写项</button>
            <button class="button" id="copy-config" type="button" disabled>复制配置</button>
            <button class="button" id="download-config" type="button" disabled>下载配置</button>
            <button class="button" id="download-config-jsonc" type="button" disabled>下载为 opencode.jsonc</button>
          </div>
          <ol class="guide" id="client-guide">
            <li>先在上方创建并安全保存 Gateway API Key。</li>
            <li>选择要导出的 Provider 和 canonical 模型。</li>
            <li>选择 OpenCode 或 Cline，页面会显示下一步。</li>
          </ol>
          <h3>手工填写项</h3>
          <p class="subtle">在客户端正常的 Provider/API Key 页面逐项填写；无需创建 Windows 环境变量。</p>
          <dl id="manual-fields" class="manual-fields"></dl>
          <pre id="config-preview" hidden></pre>
        </section>
      </div>

      <div class="footer-actions">
        <p>Sign out 只退出管理页面；Exit 会停止本目录启动的全部本地服务。</p>
        <div class="actions">
          <button class="button" id="logout" type="button">退出管理页面</button>
          <button class="button danger" id="shutdown" type="button">完整退出 Gateway</button>
        </div>
      </div>
    </section>
  </main>

<script>
(() => {
  let recoveryToken = null;
  let oauthState = null;
  let gatewayKey = null;
  let gatewayKeyId = null;
  let currentConfig = null;
  let currentConfigName = null;
  let currentConfigContainsKey = false;
  let currentConfigKeyPrefix = null;
  let allModels = [];
  let exportProviderOptions = [];
  const message = document.getElementById("message");
  const login = document.getElementById("login");
  const recovery = document.getElementById("recovery");
  const authCard = document.getElementById("auth-card");
  const signedIn = document.getElementById("signed-in");
  const keyResult = document.getElementById("key-result");
  const modelSelect = document.getElementById("model-select");
  const configPreview = document.getElementById("config-preview");
  const copyConfigButton = document.getElementById("copy-config");
  const downloadConfigButton = document.getElementById("download-config");
  const downloadJsoncButton = document.getElementById("download-config-jsonc");
  const configGatewayKey = document.getElementById("opencode-gateway-key");
  const gatewayKeyList = document.getElementById("gateway-key-list");
  const exportProviderList = document.getElementById("export-provider-list");
  const signedInControls = signedIn.querySelectorAll("input, button, select");

  const showMessage = (text, tone = "info") => {
    message.textContent = text;
    message.dataset.tone = tone;
  };
  const setStep = (id, state) => { document.getElementById(id).dataset.state = state; };
  const clearGatewayKey = () => {
    gatewayKey = null;
    gatewayKeyId = null;
    configGatewayKey.value = "";
    keyResult.textContent = "";
    keyResult.hidden = true;
  };
  const clearConfig = () => {
    if (currentConfigContainsKey && currentConfig && currentConfig.provider) {
      const provider = currentConfig.provider["personal-ai-gateway"];
      if (provider && provider.options) provider.options.apiKey = "";
    }
    currentConfig = null;
    currentConfigName = null;
    currentConfigContainsKey = false;
    currentConfigKeyPrefix = null;
    configPreview.textContent = "";
    configPreview.hidden = true;
    copyConfigButton.disabled = true;
    downloadConfigButton.disabled = true;
    downloadJsoncButton.disabled = true;
  };
  const setSignedInEnabled = enabled => {
    signedInControls.forEach(control => { control.disabled = !enabled; });
    if (enabled && !currentConfig) {
      copyConfigButton.disabled = true;
      downloadConfigButton.disabled = true;
    }
  };
  const leaveSignedIn = () => {
    clearGatewayKey();
    clearCustomProviderSecrets();
    clearConfig();
    recoveryToken = null;
    oauthState = null;
    setSignedInEnabled(false);
  };
  const request = async (url, body, method = "POST") => fetch(url, {
    method,
    credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  const readJson = async url => {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
  };
  const copyText = async value => {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_) {
      const field = document.createElement("textarea");
      field.value = value;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      const copied = document.execCommand("copy");
      field.value = "";
      field.remove();
      return copied;
    }
  };
  const humanState = state => ({
    available: "已连接", auth_required: "需要登录 Provider", running: "运行中",
    starting: "启动中", stopped: "已停止", crashed: "异常退出",
    api_auth_failed: "本地认证异常", unavailable: "暂不可用", unknown: "未知"
  }[state] || "需要检查");

  const customProviderForm = document.getElementById("custom-provider-form");
  const customModelList = document.getElementById("custom-provider-models");
  const customHeaderList = document.getElementById("custom-provider-headers");
  const customProviderList = document.getElementById("custom-provider-list");
  const manualFields = document.getElementById("manual-fields");
  const antigravityProxyField = document.getElementById("antigravity-proxy-url");
  const antigravityProxyState = document.getElementById("antigravity-proxy-state");

  const clearCustomProviderSecrets = () => {
    document.getElementById("custom-provider-api-key").value = "";
    customHeaderList.querySelectorAll('[data-header-value]').forEach(field => { field.value = ""; });
  };
  const addDynamicRow = kind => {
    const isModel = kind === "model";
    const list = isModel ? customModelList : customHeaderList;
    const row = document.createElement("div");
    row.className = "dynamic-row";
    const first = document.createElement("input");
    const second = document.createElement("input");
    first.type = "text"; second.type = "text";
    first.maxLength = second.maxLength = 512;
    first.placeholder = isModel ? "Model ID" : "Header-Name";
    second.placeholder = isModel ? "显示名称" : "value";
    first.setAttribute("aria-label", isModel ? "Model ID" : "请求头名称");
    second.setAttribute("aria-label", isModel ? "模型显示名称" : "请求头值");
    if (isModel) { first.required = true; first.dataset.modelId = ""; second.dataset.modelName = ""; }
    else { first.dataset.headerName = ""; second.dataset.headerValue = ""; }
    const remove = document.createElement("button");
    remove.type = "button"; remove.className = "button danger";
    remove.textContent = "删除";
    remove.setAttribute("aria-label", isModel ? "删除模型行" : "删除请求头行");
    remove.addEventListener("click", () => {
      row.remove();
    });
    row.append(first, second, remove);
    list.appendChild(row);
  };
  const renderManualFields = () => {
    const modelId = modelSelect.value || "请先选择模型";
    const fields = [
      ["Provider ID", "personal-ai-gateway", true],
      ["显示名称", "Personal AI Provider Gateway", true],
      ["Base URL", currentConfig && currentConfig.baseUrl ? currentConfig.baseUrl : "http://127.0.0.1:8020/v1", true],
      ["API Key", "在客户端正常的 API Key 输入框粘贴 Gateway Key", false],
      ["所选 Model ID", modelId, Boolean(modelSelect.value)],
      ["请求头", "无需填写", false]
    ];
    manualFields.textContent = "";
    fields.forEach(([label, value, copyable]) => {
      const title = document.createElement("dt"); title.textContent = label;
      const content = document.createElement("dd"); content.textContent = value;
      const row = document.createElement("div"); row.className = "manual-field";
      row.append(title, content);
      if (copyable) {
        const copy = document.createElement("button");
        copy.type = "button"; copy.className = "button"; copy.textContent = "复制";
        copy.setAttribute("aria-label", "复制" + label);
        copy.addEventListener("click", async () => {
          if (await copyText(value)) showMessage(label + "已复制。", "success");
        });
        row.appendChild(copy);
      }
      manualFields.appendChild(row);
    });
  };
  const refreshCustomProviders = async () => {
    const payload = await readJson("/api/control/providers/openai-compatible");
    const providers = Array.isArray(payload.data) ? payload.data : [];
    customProviderList.textContent = "";
    if (!providers.length) {
      const empty = document.createElement("p");
      empty.className = "subtle"; empty.textContent = "尚未导入自定义 Provider。";
      customProviderList.appendChild(empty);
      return providers;
    }
    providers.forEach(provider => {
      const summary = document.createElement("p");
      const verificationErrors = {
        provider_authentication_failed: "上游拒绝 API Key",
        provider_access_denied: "上游拒绝访问或模型权限不足",
        provider_endpoint_not_found: "上游接口地址不存在",
        provider_rate_limited: "上游限流或额度不足",
        provider_connect_timeout: "连接上游超时",
        provider_connect_error: "无法与上游建立连接",
        provider_timeout: "等待上游响应超时",
        provider_protocol_error: "上游响应格式不兼容",
        provider_unavailable: "上游网络不可用",
        provider_upstream_error: "上游服务返回错误"
      };
      const failure = verificationErrors[provider.verification_error] || "连接验证失败";
      const status = provider.verification_status === "verified"
        ? "已连接"
        : provider.verification_status === "connection_failed" ? failure + "，已保留配置" : "已配置，尚未验证";
      summary.textContent = provider.display_name + " · " + provider.provider_id + " · " + provider.model_count + " 个模型 · " + status;
      customProviderList.appendChild(summary);
    });
    return providers;
  };
  const renderModelOptions = () => {
    const enabledProviders = new Set(exportProviderOptions.filter(item => item.enabled).map(item => item.provider_id));
    const models = allModels.filter(model => enabledProviders.has(model.provider));
    const selected = modelSelect.value;
    modelSelect.textContent = "";
    if (!models.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "请至少启用一个包含可用模型的 Provider";
      modelSelect.appendChild(option);
    } else {
      models.forEach(model => {
        const option = document.createElement("option");
        option.value = model.id;
        option.textContent = model.id;
        if (model.id === selected) option.selected = true;
        modelSelect.appendChild(option);
      });
    }
    renderManualFields();
  };
  const refreshModels = async () => {
    const payload = await readJson("/api/control/models");
    allModels = Array.isArray(payload.data) ? payload.data.filter(model => model.available) : [];
    document.getElementById("model-count").textContent = String(allModels.length);
    setStep("step-key", gatewayKey ? "complete" : "active");
    renderModelOptions();
    return allModels;
  };
  const refreshExportProviders = async () => {
    const payload = await readJson("/api/control/integrations/export-providers");
    exportProviderOptions = Array.isArray(payload.data) ? payload.data : [];
    exportProviderList.textContent = "";
    if (!exportProviderOptions.length) {
      const empty = document.createElement("p");
      empty.className = "subtle";
      empty.textContent = "尚无可导出的 Provider。";
      exportProviderList.appendChild(empty);
    } else {
      exportProviderOptions.forEach(provider => {
        const label = document.createElement("label");
        label.className = "provider-choice";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = provider.provider_id;
        checkbox.checked = provider.enabled;
        checkbox.addEventListener("change", () => {
          provider.enabled = checkbox.checked;
          renderModelOptions();
        });
        label.append(checkbox, document.createTextNode(" " + provider.provider_id + " · " + provider.model_count + " 个模型"));
        exportProviderList.appendChild(label);
      });
    }
    renderModelOptions();
    return exportProviderOptions;
  };
  const formatTime = value => value ? new Date(value).toLocaleString() : "从未使用";
  const refreshGatewayKeys = async () => {
    const payload = await readJson("/api/control/gateway-keys");
    const keys = (Array.isArray(payload.data) ? payload.data : []).filter(item => item.status === "active");
    gatewayKeyList.textContent = "";
    if (!keys.length) {
      const empty = document.createElement("p");
      empty.className = "subtle"; empty.textContent = "当前没有有效的 Gateway API Key。";
      gatewayKeyList.appendChild(empty);
      return keys;
    }
    keys.forEach(item => {
      const row = document.createElement("div"); row.className = "key-item";
      const summary = document.createElement("span");
      summary.textContent = item.label + " · " + item.prefix + "… · 创建 " + formatTime(item.created_at) + " · 最近使用 " + formatTime(item.last_used_at);
      row.appendChild(summary);
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "button danger"; remove.textContent = "删除 Key";
      let armed = false;
      remove.addEventListener("click", async () => {
        if (!armed) {
          armed = true; remove.textContent = "确认删除";
          showMessage("再次点击将立即删除该 Key，使用它的客户端会立刻失去访问权限。", "warning");
          return;
        }
        remove.disabled = true;
        const response = await request("/api/control/gateway-keys/" + encodeURIComponent(item.key_id), undefined, "DELETE");
        if (!response.ok) { remove.disabled = false; showMessage("Key 删除失败，请刷新后重试。", "error"); return; }
        if (item.key_id === gatewayKeyId) clearGatewayKey();
        if (item.prefix === currentConfigKeyPrefix) clearConfig();
        await refreshGatewayKeys();
        showMessage("Key 已删除，使用它的客户端已立即失效。", "success");
      });
      row.appendChild(remove);
      gatewayKeyList.appendChild(row);
    });
    return keys;
  };
  const renderAntigravityStatus = (health, models, customProviders) => {
    const syncedCount = models.filter(model => model.provider === "antigravity" || model.id.startsWith("antigravity/")).length;
    const anyProviderConfigured = syncedCount > 0 || customProviders.length > 0 || Boolean(health && health.state === "available");
    const sidecarState = document.getElementById("sidecar-state");
    const providerState = document.getElementById("provider-state");
    const providerDot = document.getElementById("provider-dot");
    if (!health || !health.state) {
      sidecarState.textContent = "实时状态待确认";
      providerState.textContent = syncedCount ? "实时连接待确认；已有 " + syncedCount + " 个已同步模型" : "实时连接待确认";
      providerDot.className = "dot warn";
      setStep("step-provider", anyProviderConfigured ? "complete" : "active");
      return;
    }
    sidecarState.textContent = humanState(health.state);
    if (health.state === "available") {
      providerState.textContent = syncedCount ? "已连接；已有 " + syncedCount + " 个已同步模型" : "已连接；模型尚未同步";
      providerDot.className = "dot ok";
      setStep("step-provider", "complete");
      return;
    }
    providerState.textContent = syncedCount ? humanState(health.state) + "；已有 " + syncedCount + " 个历史同步模型" : humanState(health.state);
    providerDot.className = "dot warn";
    setStep("step-provider", anyProviderConfigured ? "complete" : "active");
  };
  const renderAntigravityProxyStatus = status => {
    antigravityProxyState.textContent = status && status.configured
      ? "已配置，仅 Antigravity Sidecar 上游使用；输入新地址可替换。"
      : "未配置，Antigravity 将直接连接。";
  };
  const loadDashboard = async () => {
    const [healthResult, modelsResult, providersResult, proxyResult, selectionResult, keysResult] = await Promise.allSettled([
      readJson("/api/control/sidecar/health"), refreshModels(), refreshCustomProviders(),
      readJson("/api/control/providers/antigravity/proxy"), refreshExportProviders(), refreshGatewayKeys()
    ]);
    const failures = [healthResult, modelsResult, providersResult, proxyResult, selectionResult, keysResult].filter(result => result.status === "rejected");
    if (failures.some(result => result.reason && result.reason.message === "403")) {
      if (document.body.dataset.controlMode === "password") {
        leaveSignedIn(); signedIn.hidden = true; authCard.hidden = false; login.hidden = false;
        showMessage("管理会话已过期，请重新登录。", "warning");
      } else {
        showMessage("本机访问边界校验未通过，请使用启动器打开的 127.0.0.1 地址。", "error");
      }
      return;
    }
    const models = modelsResult.status === "fulfilled" ? modelsResult.value : [];
    const providers = providersResult.status === "fulfilled" ? providersResult.value : [];
    if (modelsResult.status === "rejected") document.getElementById("model-count").textContent = "—";
    renderAntigravityStatus(healthResult.status === "fulfilled" ? healthResult.value : null, models, providers);
    renderAntigravityProxyStatus(proxyResult.status === "fulfilled" ? proxyResult.value : null);
    try {
      const summary = await readJson("/api/control/statistics/summary");
      document.getElementById("usage-count").textContent = String(summary.usage.request_count);
      document.getElementById("quota-state").textContent = summary.quota.status === "available" ? "可读取" : "未知";
    } catch (_) {
      document.getElementById("usage-count").textContent = "0";
      document.getElementById("quota-state").textContent = "未知";
    }
    if (failures.length) showMessage("部分实时状态未能刷新；模型目录和连接状态会分别显示。", "warning");
  };

  const synchronizeAntigravityModels = async () => {
    const response = await request("/api/control/providers/antigravity/models/discover");
    if (!response.ok) {
      await loadDashboard();
      showMessage("登录成功但模型同步待处理。请点击“登录完成，刷新模型”重试。", "warning");
      return false;
    }
    const data = await response.json();
    await loadDashboard();
    if (data.stale || !Array.isArray(data.models) || !data.models.length) {
      showMessage("登录成功但模型同步待处理。请点击“登录完成，刷新模型”重试。", "warning");
      return false;
    }
    showMessage("模型已同步，可以创建 Key 并配置客户端。", "success");
    return true;
  };
  document.getElementById("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    const field = document.getElementById("password");
    const response = await request("/api/control/session/login", {password: field.value});
    field.value = "";
    if (!response.ok) { showMessage("登录失败，请检查密码；忘记密码可使用下方恢复入口。", "error"); return; }
    setSignedInEnabled(true);
    login.hidden = true; recovery.hidden = true; authCard.hidden = true; signedIn.hidden = false;
    showMessage("已登录。接下来连接 Provider，然后创建客户端 Key。", "success");
    await loadDashboard();
  });

  document.getElementById("start-recovery").addEventListener("click", async () => {
    showMessage("请在 Windows 原生确认框中允许本次密码设置。", "warning");
    const response = await request("/api/control/password-recovery/challenge");
    if (!response.ok) { showMessage("未确认密码设置，或恢复请求暂不可用。可以重新尝试。", "error"); return; }
    const data = await response.json();
    recoveryToken = data.recovery_token;
    login.hidden = true; recovery.hidden = false;
    showMessage("Windows 已确认。请在两分钟内设置新密码。", "success");
  });

  document.getElementById("cancel-recovery").addEventListener("click", () => {
    recoveryToken = null;
    document.getElementById("new-password").value = "";
    recovery.hidden = true; login.hidden = false;
    showMessage("已返回登录。", "info");
  });

  document.getElementById("recovery-form").addEventListener("submit", async event => {
    event.preventDefault();
    const field = document.getElementById("new-password");
    const response = await request("/api/control/password-recovery/complete", {
      recovery_token: recoveryToken,
      new_password: field.value
    });
    field.value = ""; recoveryToken = null;
    if (!response.ok) { showMessage("确认已过期或密码不符合要求，请重新开始。", "error"); recovery.hidden = true; login.hidden = false; return; }
    recovery.hidden = true; login.hidden = false;
    showMessage("管理密码已保存。现在请用新密码登录。", "success");
  });

  document.getElementById("connect-provider").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    showMessage("正在准备 Provider 官方登录页面…", "info");
    try {
      const response = await request("/api/control/providers/antigravity/oauth/start");
      if (!response.ok) throw new Error("start");
      const data = await response.json();
      oauthState = data.oauth_state;
      const opened = window.open(data.url, "_blank");
      if (opened) opened.opener = null;
      showMessage(opened ? "请在新窗口完成 Provider 登录，然后返回本页。" : "浏览器阻止了新窗口，请允许弹窗后重新点击登录。", opened ? "success" : "warning");
      if (!opened) return;
      for (let attempt = 0; attempt < 60 && oauthState; attempt += 1) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        const statusResponse = await request("/api/control/providers/antigravity/oauth/status", {oauth_state: oauthState});
        if (!statusResponse.ok) break;
        const status = await statusResponse.json();
        if (status.state === "succeeded") {
          oauthState = null;
          await synchronizeAntigravityModels();
          return;
        }
        if (status.state === "failed") {
          oauthState = null;
          showMessage("Provider 登录未完成，可以重新尝试。", "error");
          return;
        }
      }
      if (oauthState) showMessage("仍在等待 Provider 登录。完成后点击“登录完成，刷新模型”。", "warning");
    } catch (_) {
      showMessage("无法开始 Provider 登录。请刷新状态后重试。", "error");
    } finally {
      button.disabled = false;
    }
  });

  document.getElementById("discover-models").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const response = await request("/api/control/providers/antigravity/models/discover");
      if (!response.ok) throw new Error("discover");
      const data = await response.json();
      await loadDashboard();
      showMessage(data.models.length ? "模型已同步，可以创建 Key 并配置客户端。" : "尚未发现模型，请确认 Provider 登录已完成。", data.models.length ? "success" : "warning");
    } catch (_) {
      showMessage("模型同步失败。请确认 Provider 登录完成后重试。", "error");
    } finally { button.disabled = false; }
  });

  document.getElementById("refresh-dashboard").addEventListener("click", loadDashboard);
  document.getElementById("export-provider-form").addEventListener("submit", async event => {
    event.preventDefault();
    const providerIds = Array.from(exportProviderList.querySelectorAll('input[type="checkbox"]:checked')).map(field => field.value);
    const response = await request("/api/control/integrations/export-providers", {provider_ids: providerIds});
    if (!response.ok) {
      showMessage("导出 Provider 选择保存失败，请刷新后重试。", "error");
      return;
    }
    await refreshExportProviders();
    showMessage(providerIds.length ? "导出 Provider 选择已保存。" : "已保存空选择；请至少启用一个 Provider 后再生成配置。", providerIds.length ? "success" : "warning");
  });
  document.getElementById("antigravity-proxy-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = document.getElementById("save-antigravity-proxy");
    button.disabled = true;
    try {
      const response = await request("/api/control/providers/antigravity/proxy", {
        proxy_url: antigravityProxyField.value || null
      });
      antigravityProxyField.value = "";
      if (!response.ok) {
        showMessage("代理保存失败。请填写类似 http://127.0.0.1:端口 的本地地址。", "error");
        return;
      }
      await loadDashboard();
      showMessage("Antigravity 代理已保存并应用。", "success");
    } catch (_) {
      showMessage("代理保存失败，请检查本地 Gateway。", "error");
    } finally { button.disabled = false; }
  });
  document.getElementById("clear-antigravity-proxy").addEventListener("click", async event => {
    const button = event.currentTarget;
    button.disabled = true;
    antigravityProxyField.value = "";
    try {
      const response = await request("/api/control/providers/antigravity/proxy", {proxy_url: null});
      if (!response.ok) throw new Error("clear");
      await loadDashboard();
      showMessage("Antigravity 代理已清除，将直接连接。", "success");
    } catch (_) {
      showMessage("代理清除失败，请检查本地 Gateway。", "error");
    } finally { button.disabled = false; }
  });
  document.getElementById("reload-models").addEventListener("click", async () => {
    try { await refreshModels(); showMessage("模型列表已刷新。", "success"); }
    catch (_) { showMessage("无法刷新模型，请先检查 Provider 连接。", "error"); }
  });

  document.getElementById("logout").addEventListener("click", async () => {
    leaveSignedIn();
    showMessage("正在退出。页面中的完整 Key 已清除，可以关闭此标签页。", "warning");
    const response = await request("/api/control/session/logout");
    if (!response.ok) {
      showMessage("退出请求失败，但页面中的完整 Key 已清除。可以关闭此标签页。", "error");
      return;
    }
    signedIn.hidden = true;
    authCard.hidden = false;
    login.hidden = false;
    showMessage("已退出管理页面。本地 Gateway 仍在运行。", "success");
  });

  document.getElementById("shutdown").addEventListener("click", async () => {
    leaveSignedIn();
    showMessage("正在完整退出本地 Gateway。可以关闭此标签页。", "warning");
    const response = await request("/api/control/application/shutdown");
    if (!response.ok) {
      showMessage("退出请求失败，但页面中的完整 Key 已清除。可以关闭此标签页。", "error");
    }
  });

  document.getElementById("key-form").addEventListener("submit", async event => {
    event.preventDefault();
    clearGatewayKey();
    const response = await request("/api/control/gateway-keys", {
      label: document.getElementById("key-label").value
    });
    if (!response.ok) { showMessage("Key 创建失败。客户端名称可能已使用，请换一个名称。", "error"); return; }
    const data = await response.json();
    gatewayKey = data.secret;
    gatewayKeyId = data.key_id;
    const title = document.createElement("strong");
    title.textContent = "立即复制并安全保存：";
    const secret = document.createElement("code");
    secret.textContent = gatewayKey;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "button primary";
    copy.textContent = "复制 Key";
    copy.addEventListener("click", async () => {
      if (gatewayKey && await copyText(gatewayKey)) showMessage("Key 已复制。保存后继续配置客户端。", "success");
    });
    keyResult.append(title, secret, copy);
    keyResult.hidden = false;
    setStep("step-key", "complete");
    setStep("step-client", "active");
    showMessage("Gateway API Key 已创建。它只显示这一次。", "success");
    await refreshGatewayKeys();
  });

  const showIntegration = async kind => {
    const model = modelSelect.value;
    if (!model) { showMessage("请先连接 Provider 并选择一个模型。", "warning"); return; }
    const secret = kind === "opencode" ? (configGatewayKey.value || gatewayKey) : null;
    if (kind === "opencode" && !secret) {
      showMessage("请把 Gateway API Key 粘贴到上方输入框，再生成完整 OpenCode 配置。", "warning");
      return;
    }
    const secretPrefix = secret ? secret.slice(0, 16) : null;
    if (kind === "opencode") clearGatewayKey();
    else configGatewayKey.value = "";
    clearConfig();
    try {
      const endpoint = kind === "opencode"
        ? "/api/control/integrations/opencode/config?default_model=" + encodeURIComponent(model)
        : "/api/control/integrations/cline/config?model=" + encodeURIComponent(model);
      currentConfig = await readJson(endpoint);
      currentConfigName = kind === "opencode" ? "opencode.json" : "cline-connection.json";
      if (kind === "opencode") {
        currentConfig.provider["personal-ai-gateway"].options.apiKey = secret;
        currentConfigContainsKey = true;
        currentConfigKeyPrefix = secretPrefix;
      }
      renderManualFields();
      configPreview.textContent = JSON.stringify(currentConfig, null, 2);
      configPreview.hidden = false;
      copyConfigButton.disabled = false;
      downloadConfigButton.disabled = false;
      downloadJsoncButton.disabled = kind !== "opencode";
      const guide = document.getElementById("client-guide");
      guide.textContent = "";
      const lines = kind === "opencode" ? [
        "默认把配置保存到 %USERPROFILE%\\.config\\opencode\\opencode.json；也可以改用 opencode.jsonc，但同一目录不要同时保留两份。",
        "此文件已经包含 Gateway API Key；不要上传、分享或提交到 Git。",
        "如果某个项目已有项目级 opencode.json 或 opencode.jsonc，它可能覆盖全局的同名设置。",
        "完全退出并重新打开 OpenCode，然后选择配置中显示的模型。"
      ] : [
        "在 Cline 中选择 OpenAI Compatible Provider。",
        "Base URL 填写 " + currentConfig.baseUrl + "；Model ID 填写 " + currentConfig.modelId + "。",
        "API Key 填写刚才保存的 Gateway Key，然后新建任务测试连接。"
      ];
      lines.forEach(line => { const item = document.createElement("li"); item.textContent = line; guide.appendChild(item); });
      setStep("step-client", "complete");
      showMessage(kind === "opencode" ? "含明文 Key 的 OpenCode 配置已生成。请妥善保存，勿上传或提交到 Git。" : "Cline 手动填写项已生成。", "success");
    } catch (_) {
      showMessage("配置生成失败。请先刷新模型列表。", "error");
    }
  };

  document.getElementById("show-opencode").addEventListener("click", () => showIntegration("opencode"));
  document.getElementById("show-cline").addEventListener("click", () => showIntegration("cline"));
  copyConfigButton.addEventListener("click", async () => {
    if (currentConfig && await copyText(JSON.stringify(currentConfig, null, 2))) {
      showMessage(currentConfigContainsKey ? "配置已复制，其中包含明文 Gateway API Key，请勿上传、分享或提交到 Git。" : "配置已复制。", "success");
    }
  });
  const downloadCurrentConfig = name => {
    if (!currentConfig || !name) return;
    const blob = new Blob([JSON.stringify(currentConfig, null, 2) + "\n"], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url; link.download = name; link.hidden = true;
    document.body.appendChild(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    showMessage(currentConfigContainsKey ? "配置文件已下载，其中包含明文 Gateway API Key，请勿上传、分享或提交到 Git。" : "配置文件已下载。", "success");
  };
  downloadConfigButton.addEventListener("click", () => downloadCurrentConfig(currentConfigName));
  downloadJsoncButton.addEventListener("click", () => downloadCurrentConfig("opencode.jsonc"));

  document.getElementById("add-custom-model").addEventListener("click", () => addDynamicRow("model"));
  document.getElementById("add-custom-header").addEventListener("click", () => addDynamicRow("header"));
  customProviderForm.addEventListener("submit", async event => {
    event.preventDefault();
    const submit = document.getElementById("submit-custom-provider");
    const apiKeyField = document.getElementById("custom-provider-api-key");
    const modelRows = Array.from(customModelList.querySelectorAll(".dynamic-row"));
    const headerRows = Array.from(customHeaderList.querySelectorAll(".dynamic-row"));
    const payload = {
      provider_id: document.getElementById("custom-provider-id").value,
      display_name: document.getElementById("custom-provider-name").value,
      base_url: document.getElementById("custom-provider-base-url").value,
      api_key: apiKeyField.value || null,
      models: modelRows.map(row => ({
        model_id: row.querySelector("[data-model-id]").value,
        display_name: row.querySelector("[data-model-name]").value || null
      })),
      headers: headerRows.map(row => ({
        name: row.querySelector("[data-header-name]").value,
        value: row.querySelector("[data-header-value]").value
      })).filter(header => header.name || header.value)
    };
    submit.disabled = true;
    showMessage("正在安全导入 Provider…", "info");
    try {
      const response = await request("/api/control/providers/openai-compatible", payload);
      if (!response.ok) {
        showMessage("Provider 导入失败。请检查地址、模型和请求头后重试。", "error");
        return;
      }
      const data = await response.json();
      await Promise.all([refreshModels(), refreshCustomProviders(), refreshExportProviders()]);
      if (data.verification_status === "verified") {
        showMessage(data.display_name + " 已导入并验证，共 " + data.model_count + " 个可用模型。", "success");
      } else {
        document.getElementById("custom-provider-model-fallback").open = true;
        showMessage(data.display_name + " 已保存，但自动同步模型失败；如需立即使用，请展开备用项手动添加模型。", "warning");
      }
    } catch (_) {
      showMessage("Provider 导入失败。请检查本地 Gateway 后重试。", "error");
    } finally {
      payload.api_key = null;
      payload.headers.forEach(header => { header.value = ""; });
      clearCustomProviderSecrets();
      submit.disabled = false;
    }
  });
  modelSelect.addEventListener("change", renderManualFields);
  addDynamicRow("header");
  renderManualFields();
  if (document.body.dataset.controlMode === "direct") {
    authCard.hidden = true;
    login.hidden = true;
    recovery.hidden = true;
    signedIn.hidden = false;
    document.getElementById("logout").hidden = true;
    setSignedInEnabled(true);
    showMessage("已打开本机管理页面。连接 Provider 后即可创建 Key 和配置客户端。", "success");
    loadDashboard();
  } else {
    showMessage("请登录本机管理页面。", "info");
  }
  window.addEventListener("pagehide", clearCustomProviderSecrets);
  window.addEventListener("pagehide", clearGatewayKey);
  window.addEventListener("pagehide", clearConfig);
})();
</script>
</body>
</html>
"""
