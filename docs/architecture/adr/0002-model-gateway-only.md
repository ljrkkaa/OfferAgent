# ADR-0002：模型只能通过 ModelGateway 推理

状态：Accepted

## 决策

DeepSeek/Codex/OpenAI/兼容/本地模型只实现 `ModelGateway`。Provider 接收规范化请求并产生结构化
`ModelEvent`；它不知道 cwd、Vault、工具、Session、审批、Memory 或 Subagent。

DeepSeek 使用固定的官方 Chat Completions 推理端点。Adapter 不发送 Provider 原生工具；JSON 模式只把
Harness 的 canonical JSON Schema 作为受信 system 约束发送，并在本机用原始 Draft 2020-12 Schema
再次严格校验。DeepSeek 流中的原始 `reasoning_content` 是思维链，不是摘要，必须在 Provider 边界
丢弃；只允许 reasoning token 计数进入 `ModelUsage`。

## 后果

不启动 `codex app-server`，不复用 Codex CLI 工具。当前正式 Provider 请求不发送任何远程工具定义；
若 Provider 返回未请求的 tool call，Adapter fail closed。所有 Provider 凭据只通过 Workspace 绑定的
opaque `SecretHandle` 在 Windows DPAPI SecretStore 内消费，执行权始终属于 Tool Kernel。
