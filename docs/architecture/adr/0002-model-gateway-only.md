# ADR-0002：模型只能通过 ModelGateway 推理

状态：Accepted

## 决策

Codex/OpenAI/兼容/本地模型只实现 `ModelGateway`。Provider 接收规范化请求并产生结构化 ModelEvent；它不知道 cwd、Vault、工具、Session、审批、Memory、MCP 或 Subagent。

## 后果

不启动 `codex app-server`，不复用 Codex CLI 工具。Provider 原生 function calling 只能作为编码优化并转换成 canonical ToolCall，执行权始终属于 Tool Kernel。

