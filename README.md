# CCSwitch Antigravity Provider

把用户已授权的 Antigravity 能力作为本地 Provider 交给 [CC Switch](https://github.com/farion1231/cc-switch) 管理。

> 当前状态：仓库初始化完成，功能尚未实现，暂无可下载 Release。请勿把当前内容当作可用产品。

## 定位

```text
CC Switch → Antigravity Bridge → CLIProxyAPI Sidecar → Antigravity
```

CC Switch 负责下游客户端、Provider 切换和模型配置；本项目只负责 Antigravity OAuth、Sidecar 生命周期、上游连通和必要的 OpenAI-compatible Bridge。

首版仅面向 Windows 本地单用户，支持 Antigravity 专用的可选本地 HTTP 代理。它不依赖或执行 `Antigravity.cmd`，不捆绑代理软件、节点或订阅，也不支持含账号密码的代理。

本项目不再维护 Cline、通用自定义 Provider 聚合或完整 Gateway Dashboard。

## 当前工作

下一步是完成 CC Switch 集成契约和最小 PoC。详见 [项目规格](PROJECT_SPEC.md)与[开发阶段](docs/development-phases.md)。

## 旧项目

独立 Gateway 的历史保留在 [Personal AI Provider Gateway](https://github.com/TwinJades/Personal-AI-Provider-Gateway)，最后一个独立 Gateway Beta 为 [v0.2.0-beta.1](https://github.com/TwinJades/Personal-AI-Provider-Gateway/releases/tag/v0.2.0-beta.1)。旧项目的功能和验收结论不自动适用于本项目。

## 安全

不要提交或在 Issue 中粘贴任何密码、API Key、Management Key、OAuth Credential、代理秘密或 Provider 凭据。

## License

[MIT](LICENSE)
