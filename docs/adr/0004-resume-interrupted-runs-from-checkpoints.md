---
status: accepted
---

# Resume interrupted runs from durable checkpoints

OfferAgent 自动恢复 Conversation 历史，但不会在启动时自动继续已经持久化为 terminal Interrupted 的 Run。用户明确选择“继续”后，Python Worker 以该 terminal Run 和原 Turn 为耐久来源创建新的 root Run attempt；只有来源没有 attempted、committed、partial 或 unknown 外部副作用时才允许这样做。来源 Run、部分回答和事件保持不可变，新 attempt 不继承未完成的模型文本。这个设计避免意外消耗模型额度和重复工具副作用，也不把插件内存伪装成恢复点。

进程崩溃留下的 non-terminal Run 属于另一条启动恢复路径：Worker 只能在持久化 checkpoint 与工具调用账本能证明精确恢复时继续同一 Run；无法证明时必须先持久化 Interrupted/unknown outcome，而不能猜测执行进度。

## Consequences

- Runtime 必须持久化 Agent Run 状态、工具调用、工具结果、待审核 Vault Action 和幂等标识。
- 已提交的工具副作用不得在恢复时重复执行。
- 中断发生在模型流式输出中间时，未完成片段不构成 Run Checkpoint；它只用于重放显示，并且不会进入后续 Conversation Context。
- 插件重新连接时恢复 Conversation 和 Interrupted Run 提示，但只有用户操作才能继续执行。
- 插件的“继续”只发送 `turn/retry` 命令；安全判断、attempt 身份和执行都由 Python Worker 拥有。
