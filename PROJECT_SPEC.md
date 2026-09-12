# CCSwitch Antigravity Provider

本文件是新项目的权威状态入口。

## 项目目标

将 Antigravity 封装为 CC Switch 可管理的本地 Provider。CC Switch 负责下游，本项目只负责 Antigravity OAuth、CLIProxyAPI Sidecar、上游连接和必要的 OpenAI-compatible Bridge。

## 当前阶段

`Phase 0 — 仓库初始化完成；CC Switch 集成契约与最小 PoC 尚待完成。`

当前没有可用发行包，也不继承旧 Gateway 的发布状态。阶段详情见 [开发阶段](docs/development-phases.md)。

## 当前允许事项

1. 验证 CC Switch 的 Provider 配置、模型发现和请求转发契约。
2. 从旧 Gateway 提取 Antigravity、CLIProxyAPI、OAuth、代理和安全边界的最小可复用实现。
3. 建立针对性测试与本机 PoC。

## 当前禁止事项

1. 整体复制旧 Gateway 或继续构建通用 Provider Gateway。
2. 实现 Cline、学校 API或其他自定义 Provider 聚合。
3. 捆绑代理软件、节点或订阅，扫描端口，读取或执行 `Antigravity.cmd`。
4. 支持含用户名或密码的代理。
5. 未经验收就创建 Release 或宣称可用。
6. 修改、替换或冒用旧项目 Release 和验收结论。

## 架构与边界

```text
CC Switch
   ↓ Provider URL + Bridge API Key
Antigravity Bridge
   ↓ loopback
CLIProxyAPI Sidecar
   ↓ OAuth / optional local HTTP proxy
Antigravity
```

- CC Switch 是下游控制中心。
- Bridge 不读取工程文件、不执行工程命令。
- OAuth Credential 由 Sidecar 管理。
- 可选代理只作用于 Antigravity 上游；loopback 始终直连。
- OAuth Credential、Bridge API Key 与 Management Key 是独立凭据域。
- Browser 和 CC Switch 不得获得 Management Key。
- 第三方 Sidecar 必须固定可信版本并保留许可证和可核验来源。
- 未经验证的能力必须标记为待验证。
- 每阶段完成后更新文档并停止，不自动进入下一阶段。

## 文档索引

- [README](README.md)：面向使用者的定位和当前状态。
- [开发阶段](docs/development-phases.md)：阶段范围、完成和停止条件。
- [Agent 入口](AGENTS.md)：新 Agent 必读顺序与安全边界。
