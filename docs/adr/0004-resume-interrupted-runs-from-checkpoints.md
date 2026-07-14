---
status: accepted
---

# Resume interrupted runs from durable checkpoints

OfferAgent 自动恢复 Conversation 历史，但不会在启动时自动继续 Interrupted Run；用户明确选择继续后，Runtime 从最后一个 Run Checkpoint 恢复，而不是重新发送整条用户请求。这个设计避免意外消耗模型额度和重复工具副作用，同时保留长任务在插件重载或进程退出后的连续性。

## Consequences

- Runtime 必须持久化 Agent Run 状态、工具调用、工具结果、待审核 Vault Action 和幂等标识。
- 已提交的工具副作用不得在恢复时重复执行。
- 中断发生在模型流式输出中间时，未完成片段不构成 Run Checkpoint，恢复时重新执行该模型步骤。
- 插件重新连接时恢复 Conversation 和 Interrupted Run 提示，但只有用户操作才能继续执行。
