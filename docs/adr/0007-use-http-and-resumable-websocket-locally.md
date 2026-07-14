---
status: accepted
---

# Use HTTP management endpoints and a resumable WebSocket locally

插件与 Runtime 之间使用少量 HTTP 管理接口和一条长期、双向、可恢复的 WebSocket。HTTP 只承载健康检查、模型列表和静态配置；Conversation、Agent Run、流式模型输出、工具调用、工具结果、Vault Action 和恢复事件都通过 WebSocket 传输，因为 Runtime 必须能够主动请求插件执行 Vault 能力。

## Consequences

- 所有 WebSocket 消息都必须带协议版本、事件标识、Conversation 标识、Agent Run 标识和单调递增序号。
- Runtime 在提交 Run Event 后持久化它，插件确认最后处理序号；重连时只重放未确认事件。
- 重复事件必须通过事件标识和工具幂等标识安全去重。
- HTTP 和 WebSocket 都只能绑定本机地址，并要求本地认证令牌。
