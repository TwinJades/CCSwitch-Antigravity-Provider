# Development Phases

## 当前状态

Phase 0：仓库初始化已完成；CC Switch 集成契约和最小 PoC 尚未完成。

## Phase 0 — 集成契约与 PoC

范围：

- 确认 CC Switch 创建 Provider、获取并保存模型、转发请求所需的真实契约。
- 验证它能把本地 Bridge 当作普通 OpenAI-compatible Provider。
- 明确认证、模型 ID、流式响应、错误传递与生命周期边界。
- 记录旧 Gateway 中可复用和必须舍弃的模块。

完成条件：使用无真实秘密的测试配置完成模型发现、非流式与流式请求 PoC，并形成最小接口和安全记录。

停止条件：完成 PoC 与文档后停止；未经授权不进入 Phase 1。

## Phase 1 — 最小 Antigravity Bridge

计划提取 CLIProxyAPI 生命周期、OAuth、健康状态和固定版本完整性管理，实现 CC Switch 所需的最小 Provider 接口，并支持仅作用于 Antigravity 上游的可选本地 HTTP 代理。

Phase 1 尚未授权，也未开始。打包、真实设备验收和公开 Beta 必须另行定义，不继承旧 Gateway 的阶段编号、发行包或验收结论。
